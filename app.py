"""ANENGOS 极简 HTTP 服务：真实模型 + 治理闭环 + Web 管理台，供 Docker 一键部署。

端点：
  GET  /health                  存活检查（Docker healthcheck 用）
  POST /run                     {"query": "..."} 跑一个受治理的 agent 任务（需鉴权）
  GET  /                        管理台页面（admin.html）
  POST /admin/api/run           管理台任务入口（同 /run，需鉴权）
  GET  /admin/api/approvals     待审批队列（需鉴权）
  POST /admin/api/approvals/{id}/approve|reject   批准（真实执行）/ 拒绝（需鉴权）
  POST /admin/api/approvals/approve-all           批量批准
  GET  /admin/api/audit         最近审计日志（需鉴权）
  GET  /admin/api/files         工作区文件列表（需鉴权）

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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from governance.approval import ApprovalItem, ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.llm import OpenAICompatLLM
from kernel.loop import AgentOS
from kernel.tools import ToolRegistry, build_default_tools

ACTOR = "anengos-api"
BASE = Path(os.environ.get("ANENGOS_HOME", "."))
WORKSPACE = BASE / "workspace"
AUDIT_FILE = BASE / "audit" / "audit.jsonl"
ADMIN_HTML = BASE / "admin.html"

# 全局单例：审批队列与工具注册表跨请求存续（simulate-first 审批闭环依赖它，
# 否则 /run 返回的 pending_approvals 在请求结束后就丢失，无法在管理台批准）。
_APPROVALS: ApprovalQueue | None = None
_TOOLS: ToolRegistry | None = None


def _api_token() -> str:
    return os.environ.get("ANENGOS_API_TOKEN", "")


def _check_auth(headers) -> tuple[bool, str]:
    """校验访问令牌；未配置令牌时拒绝对外运行接口。"""
    token = _api_token()
    if not token:
        return False, "服务未配置 ANENGOS_API_TOKEN，运行接口已禁用（防裸奔）；设置令牌后重启"
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


def _shared_context() -> tuple[ApprovalQueue, ToolRegistry]:
    """审批队列与工具注册表全局单例。"""
    global _APPROVALS, _TOOLS
    WORKSPACE.mkdir(parents=True, exist_ok=True)  # 工具真实执行前确保工作区存在
    if _APPROVALS is None:
        _APPROVALS = ApprovalQueue()
    if _TOOLS is None:
        _TOOLS = build_default_tools(WORKSPACE)
    return _APPROVALS, _TOOLS


def _new_audit() -> AuditLog:
    return AuditLog(AUDIT_FILE)


def _apply_approval(item: ApprovalItem) -> str:
    """批准后真实执行副作用动作，并留审计。"""
    tools = _shared_context()[1]
    result = tools.run(item.tool, item.args)
    _new_audit().log(
        {
            "actor": item.actor,
            "event": "approval_applied",
            "tool": item.tool,
            "args": item.args,
            "allowed": True,
            "decision": f"已批准并真实执行: {result}",
        }
    )
    return result


def _reject_approval(item: ApprovalItem) -> None:
    _new_audit().log(
        {
            "actor": item.actor,
            "event": "approval_rejected",
            "tool": item.tool,
            "args": item.args,
            "allowed": False,
            "decision": "管理员拒绝",
        }
    )


def build_agent() -> tuple[AgentOS | None, ApprovalQueue, str]:
    """组装受治理的 agent；未配置 API key 时返回错误信息。"""
    registry = CapabilityRegistry()
    registry.introduce(ACTOR, "file.read", "workspace", side_effect=False)
    registry.introduce(ACTOR, "file.write", "workspace", side_effect=True)
    registry.introduce(ACTOR, "file.list", "workspace", side_effect=False)

    approvals, tools = _shared_context()
    audit = _new_audit()
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


def _run_task(query: str) -> tuple[int, dict[str, Any]]:
    agent, approvals, err = build_agent()
    if agent is None:
        return 503, {"error": err}
    session = agent.run(query, max_steps=20)
    return 200, {
        "output": session.output,
        "blocked": session.blocked_reason,
        "pending_approvals": [
            {"id": a.request_id, "tool": a.tool} for a in approvals.pending()
        ],
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

    def _html(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _require_auth(self) -> tuple[bool, str | None]:
        ok, reason = _check_auth(self.headers)
        if not ok:
            code = 503 if not _api_token() else 401
            self._json(code, {"error": reason})
            return False, reason
        return True, None

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._json(200, _health_body())
            return
        if path in ("/", "/admin"):
            try:
                self._html(200, ADMIN_HTML.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>admin.html 缺失</p>")
            return
        if path == "/admin/api/approvals":
            if not self._require_auth()[0]:
                return
            self._json(
                200,
                {
                    "pending": [
                        {
                            "id": a.request_id,
                            "tool": a.tool,
                            "args": a.args,
                            "simulated_result": a.simulated_result,
                        }
                        for a in _shared_context()[0].pending()
                    ]
                },
            )
            return
        if path == "/admin/api/audit":
            if not self._require_auth()[0]:
                return
            rows = []
            try:
                for line in AUDIT_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            except FileNotFoundError:
                rows = []
            self._json(200, {"audit": rows[-50:]})
            return
        if path == "/admin/api/files":
            if not self._require_auth()[0]:
                return
            files = []
            if WORKSPACE.exists():
                for p in sorted(WORKSPACE.iterdir()):
                    if p.is_file():
                        files.append({"name": p.name, "size": _human_size(p.stat().st_size)})
            self._json(200, {"files": files})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path

        if path == "/run":
            if not self._require_auth()[0]:
                return
            query = self._read_query()
            if query is None:
                return
            self._json(*_run_task(query))
            return

        if path == "/admin/api/run":
            if not self._require_auth()[0]:
                return
            query = self._read_query()
            if query is None:
                return
            self._json(*_run_task(query))
            return

        if path == "/admin/api/approvals/approve-all":
            if not self._require_auth()[0]:
                return
            approvals = _shared_context()[0]
            items = [i for i in approvals.pending()]
            for item in items:
                approvals.approve(item.request_id)
                _apply_approval(item)
            self._json(200, {"approved": len(items)})
            return

        # /admin/api/approvals/{id}/approve | reject
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "approvals":
            if not self._require_auth()[0]:
                return
            req_id, action = parts[3], parts[4]
            approvals = _shared_context()[0]
            item = approvals.get(req_id)
            if item is None:
                self._json(404, {"error": f"审批项不存在: {req_id}"})
                return
            if action == "approve":
                if not approvals.approve(req_id):
                    self._json(409, {"error": "该审批项已处理"})
                    return
                result = _apply_approval(item)
                self._json(200, {"message": "已批准并真实执行", "result": result})
                return
            if action == "reject":
                if not approvals.reject(req_id):
                    self._json(409, {"error": "该审批项已处理"})
                    return
                _reject_approval(item)
                self._json(200, {"message": "已拒绝"})
                return
            self._json(400, {"error": f"未知动作: {action}"})
            return

        self._json(404, {"error": "not found"})

    def _read_query(self) -> str | None:
        """读取并校验 {"query": "..."}，非法返回 None（已回错误响应）。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            query = str(payload.get("query", "")).strip()
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "body 需为 JSON: {\"query\": \"...\"}"})
            return None
        if not query:
            self._json(400, {"error": "query 不能为空"})
            return None
        return query


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def main() -> None:
    port = int(os.environ.get("ANENGOS_PORT", "8080"))
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (BASE / "audit").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"ANENGOS listening on :{port}（health: /health, run: POST /run, 管理台: /）",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
