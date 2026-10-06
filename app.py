"""ANENGOS 极简 HTTP 服务：真实模型 + 治理闭环，供 Docker 一键部署。

端点：
  GET  /health          存活检查（Docker healthcheck 用）
  POST /run             {"query": "..."} 跑一个受治理的 agent 任务（需鉴权）

配置：
  ANENGOS_API_KEY / ANENGOS_BASE_URL / ANENGOS_MODEL / ANENGOS_PORT（默认 8080）
  ANENGOS_API_TOKEN  访问令牌：未配置时 /run 拒绝对外服务；配置后请求必须带
                     Authorization: Bearer <token> 或 X-API-Token: <token>
审计默认写入 ./audit/audit.jsonl，工作区默认 ./workspace。
"""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.llm import OpenAICompatLLM
from kernel.loop import AgentOS
from kernel.tools import build_default_tools

ACTOR = "anengos-api"
BASE = Path(os.environ.get("ANENGOS_HOME", "."))
WORKSPACE = BASE / "workspace"
AUDIT_FILE = BASE / "audit" / "audit.jsonl"


def _api_token() -> str:
    return os.environ.get("ANENGOS_API_TOKEN", "")


def _check_auth(headers) -> tuple[bool, str]:
    """校验访问令牌；未配置令牌时拒绝对外运行接口。"""
    token = _api_token()
    if not token:
        return False, "服务未配置 ANENGOS_API_TOKEN，/run 已禁用（防裸奔）；设置令牌后重启"
    provided = headers.get("Authorization", "")
    if provided.startswith("Bearer "):
        provided = provided[7:]
    else:
        provided = headers.get("X-API-Token", "")
    if not provided:
        return False, "缺少访问令牌：请带 Authorization: Bearer <token>"
    if not hmac.compare_digest(provided, token):
        return False, "访问令牌错误"
    return True, ""


def build_agent() -> tuple[AgentOS, ApprovalQueue, str]:
    """组装受治理的 agent；未配置 API key 时返回错误信息。"""
    registry = CapabilityRegistry()
    registry.introduce(ACTOR, "file.read", "workspace", side_effect=False)
    registry.introduce(ACTOR, "file.write", "workspace", side_effect=True)
    registry.introduce(ACTOR, "file.list", "workspace", side_effect=False)

    approvals = ApprovalQueue()
    audit = AuditLog(AUDIT_FILE)
    tools = build_default_tools(WORKSPACE)
    try:
        llm = OpenAICompatLLM()
    except ValueError as e:
        return None, approvals, str(e)
    os_ = AgentOS(ACTOR, tools, Gatekeeper(registry, "api"), approvals, audit, llm)
    return os_, approvals, ""


def _health_body() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "anengos",
        "has_api_key": bool(os.environ.get("ANENGOS_API_KEY", "")),
        "auth_required": bool(_api_token()),
        "model": os.environ.get("ANENGOS_MODEL", "gpt-4o-mini"),
        "workspace": str(WORKSPACE),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静默访问日志
        pass

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, _health_body())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._json(404, {"error": "not found"})
            return
        ok, reason = _check_auth(self.headers)
        if not ok:
            code = 503 if not _api_token() else 401
            self._json(code, {"error": reason})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            query = str(payload.get("query", "")).strip()
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "body 需为 JSON: {\"query\": \"...\"}"})
            return
        if not query:
            self._json(400, {"error": "query 不能为空"})
            return

        agent, approvals, err = build_agent()
        if agent is None:
            self._json(503, {"error": err})
            return
        session = agent.run(query, max_steps=20)
        self._json(
            200,
            {
                "output": session.output,
                "blocked": session.blocked_reason,
                "pending_approvals": [
                    {"id": a.request_id, "tool": a.tool} for a in approvals.pending()
                ],
            },
        )


def main() -> None:
    port = int(os.environ.get("ANENGOS_PORT", "8080"))
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (BASE / "audit").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"ANENGOS listening on :{port}（health: /health, run: POST /run）", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
