"""管理台与审批闭环测试：页面可访问、admin API 鉴权、批准后真实执行副作用。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


class _FakeLLM:
    """模拟真实模型：先调用 file_write 工具，再结束回合。"""

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
                        "id": "r1",
                        "name": "file_write",  # 模型返回规范化名（兼容 DeepSeek/OpenAI）
                        "input": {"path": "hello_admin.txt", "content": "来自管理台"},
                    }
                ],
            }
        return {"stop_reason": "end_turn", "text": "已完成写入并等待审批"}


def _start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


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


def _reset_state(monkeypatch, tmp_path):
    """把工作区/管理台页面指向 tmp，重置全局审批与工具单例。"""
    monkeypatch.setattr(app, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit.jsonl")
    html = tmp_path / "admin.html"
    html.write_text("<title>ANENGOS 管理台</title>", encoding="utf-8")
    monkeypatch.setattr(app, "ADMIN_HTML", html)
    monkeypatch.setattr(app, "_APPROVALS", None)
    monkeypatch.setattr(app, "_TOOLS", None)
    monkeypatch.setattr(app, "OpenAICompatLLM", _FakeLLM)


def test_admin_html_ships_in_repo():
    """管理台页面文件必须随仓库分发（Dockerfile COPY admin.html 依赖它）。"""
    assert (Path(__file__).resolve().parent.parent / "admin.html").exists()


def test_admin_page_served(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    srv, url = _start_server()
    try:
        # / 为产品主页（含定价）
        code, body = _request(url + "/")
        assert code == 200
        assert "ANENGOS" in body and "定价" in body
        # /admin 为管理台
        code, body = _request(url + "/admin")
        assert code == 200
        assert "ANENGOS" in body and "管理台" in body
    finally:
        srv.shutdown()


def test_admin_api_requires_token(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    srv, url = _start_server()
    try:
        for ep in ["admin/api/approvals", "admin/api/audit", "admin/api/files"]:
            code, _ = _request(url + "/" + ep)
            assert code == 401, ep
    finally:
        srv.shutdown()


def test_approval_loop_applies_side_effect(monkeypatch, tmp_path):
    """完整审批闭环：任务产生待批 -> 管理台批准 -> 文件真实落盘 -> 审计留痕。"""
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    srv, url = _start_server()
    try:
        # 1. 跑任务：模型调用 file.write，进入审批队列
        code, body = _request(url + "/run", method="POST", token="secret123",
                              payload={"query": "写入 hello_admin.txt"})
        assert code == 200
        assert body["pending_approvals"] == [{"id": "req-1", "tool": "file.write"}]

        # 2. 审批队列可见
        code, body = _request(url + "/admin/api/approvals", token="secret123")
        assert code == 200
        assert len(body["pending"]) == 1
        assert body["pending"][0]["tool"] == "file.write"

        # 3. 批准 -> 真实执行
        code, body = _request(url + "/admin/api/approvals/req-1/approve",
                              method="POST", token="secret123")
        assert code == 200
        assert "已批准" in body["message"]
        assert (tmp_path / "ws" / "hello_admin.txt").read_text(encoding="utf-8") == "来自管理台"

        # 4. 队列清空 + 审计留痕 + 文件列表可见
        code, body = _request(url + "/admin/api/approvals", token="secret123")
        assert body["pending"] == []
        code, body = _request(url + "/admin/api/audit", token="secret123")
        events = [r["event"] for r in body["audit"]]
        assert "approval_applied" in events
        code, body = _request(url + "/admin/api/files", token="secret123")
        assert any(f["name"] == "hello_admin.txt" for f in body["files"])
    finally:
        srv.shutdown()


def test_reject_approval(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    srv, url = _start_server()
    try:
        _request(url + "/run", method="POST", token="secret123",
                 payload={"query": "写入 hello_admin.txt"})
        code, body = _request(url + "/admin/api/approvals/req-1/reject",
                              method="POST", token="secret123")
        assert code == 200
        assert not (tmp_path / "ws" / "hello_admin.txt").exists()
        code, body = _request(url + "/admin/api/audit", token="secret123")
        assert "approval_rejected" in [r["event"] for r in body["audit"]]
    finally:
        srv.shutdown()
