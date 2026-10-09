"""多智能体总装线：Codex 适配器测试（mock 模式全链路 + 治理闭环）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app
from connectors.codex import CodexAdapter


class _FakeLLM:
    """模拟真实模型：先调用 codex.submit 工具，再结束回合。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, schemas):
        self.calls += 1
        if self.calls == 1:
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "codex_submit",
                        "input": {"task": "为项目编写一个 Hello ANENGOS 示例", "repo": "demo"},
                    }
                ],
            }
        return {"stop_reason": "end_turn", "text": "已向 Codex 提交任务"}


def _request(url: str, method: str = "GET", token: str | None = None, payload=None):
    req = urllib.request.Request(url, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw.startswith("{") else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw.startswith("{") else raw


def test_mock_adapter_produces_file(tmp_path):
    """mock 模式：submit 在工作区产出交付物文件。"""
    adapter = CodexAdapter(mode="mock")
    result = adapter.submit("写一个 hello.py", str(tmp_path))
    assert "codex" in result
    out = tmp_path / "codex_output"
    assert out.exists()
    files = list(out.glob("*.md"))
    assert len(files) == 1
    assert "Hello" in files[0].read_text(encoding="utf-8") or "演示模式" in files[0].read_text(encoding="utf-8")


def test_codex_tool_governance_loop(monkeypatch, tmp_path):
    """总装线链路：模型指挥 Codex -> 审批 -> 真实执行 -> 工作区交付物 + 审计。"""
    monkeypatch.setattr(app, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(app, "TENANTS_FILE", tmp_path / "tenants.json")
    html = tmp_path / "admin.html"
    html.write_text("<title>ANENGOS 管理台</title>", encoding="utf-8")
    monkeypatch.setattr(app, "ADMIN_HTML", html)
    monkeypatch.setattr(app, "_APPROVALS", None)
    monkeypatch.setattr(app, "_TOOLS", None)
    monkeypatch.setattr(app, "_TENANTS", {})
    monkeypatch.setattr(app, "_TENANT_QUEUES", {})
    monkeypatch.setattr(app, "OpenAICompatLLM", _FakeLLM)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, body = _request(url + "/run", "POST", "admin123", {"query": "让 Codex 写示例"})
        assert code == 200
        assert body["pending_approvals"] == [{"id": "req-1", "tool": "codex.submit"}]

        code, body = _request(url + "/admin/api/approvals/req-1/approve", "POST", "admin123")
        assert code == 200
        assert "codex" in body.get("result", "").lower()

        # 交付物落在工作区
        assert (tmp_path / "ws" / "codex_output").exists()
        assert list((tmp_path / "ws" / "codex_output").glob("*.md"))

        # 审计留痕
        code, body = _request(url + "/admin/api/audit", token="admin123")
        events = [r["event"] for r in body["audit"]]
        assert "tool_call" in events and "approval_applied" in events
        tools = [r["tool"] for r in body["audit"]]
        assert "codex.submit" in tools
    finally:
        srv.shutdown()
