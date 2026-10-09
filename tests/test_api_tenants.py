"""多租户测试：租户创建/停用、token 隔离、工作区隔离、审批/审计隔离、权限控制。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

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
                        "name": "file_write",
                        "input": {"path": "note.txt", "content": "租户文件"},
                    }
                ],
            }
        return {"stop_reason": "end_turn", "text": "完成"}


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


def _reset(monkeypatch, tmp_path):
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


def test_admin_creates_tenant(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _start_server()
    try:
        code, body = _request(url + "/admin/api/tenants", method="POST", token="admin123",
                              payload={"name": "客户A"})
        assert code == 200
        assert body["tenant_id"]
        assert body["token"]  # 明文 token 一次性返回
        assert body["name"] == "客户A"
        # 列表可见
        code, body = _request(url + "/admin/api/tenants", token="admin123")
        assert code == 200
        assert len(body["tenants"]) == 1
        assert body["tenants"][0]["name"] == "客户A"
        assert body["tenants"][0]["status"] == "active"
    finally:
        srv.shutdown()


def test_tenant_run_isolated_workspace(monkeypatch, tmp_path):
    """租户 token 跑任务：文件写入租户自己的工作区，审批/审计走租户自己的通道。"""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _start_server()
    try:
        code, body = _request(url + "/admin/api/tenants", method="POST", token="admin123",
                              payload={"name": "客户B"})
        t_token = body["token"]
        tid = body["tenant_id"]

        # 租户跑任务
        code, body = _request(url + "/run", method="POST", token=t_token,
                              payload={"query": "写文件"})
        assert code == 200
        assert body["pending_approvals"]

        # 租户批准 -> 文件落到租户工作区
        code, body = _request(url + "/admin/api/approvals/req-1/approve",
                              method="POST", token=t_token)
        assert code == 200
        assert (tmp_path / "ws" / "tenants" / tid / "note.txt").read_text(encoding="utf-8") == "租户文件"

        # 租户审计独立：管理员审计文件为空，租户审计文件有记录
        assert not (tmp_path / "audit.jsonl").exists() or \
            (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip() == ""
        tenant_audit = tmp_path / "tenants" / tid / "audit.jsonl"  # 基于 AUDIT_FILE.parent
        assert tenant_audit.exists()
        assert "approval_applied" in tenant_audit.read_text(encoding="utf-8")
    finally:
        srv.shutdown()


def test_tenants_isolated_from_each_other(monkeypatch, tmp_path):
    """两个租户互不可见：审批队列、文件、审计各自独立。"""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _start_server()
    try:
        code, b1 = _request(url + "/admin/api/tenants", method="POST", token="admin123",
                            payload={"name": "客户X"})
        code, b2 = _request(url + "/admin/api/tenants", method="POST", token="admin123",
                            payload={"name": "客户Y"})
        t1, tid1 = b1["token"], b1["tenant_id"]
        t2, tid2 = b2["token"], b2["tenant_id"]

        _request(url + "/run", method="POST", token=t1, payload={"query": "写文件"})
        # X 有 1 个待批，Y 应该看不到
        code, body = _request(url + "/admin/api/approvals", token=t1)
        assert len(body["pending"]) == 1
        code, body = _request(url + "/admin/api/approvals", token=t2)
        assert body["pending"] == []
        # X 的文件 Y 看不到
        _request(url + "/admin/api/approvals/req-1/approve", method="POST", token=t1)
        code, body = _request(url + "/admin/api/files", token=t1)
        assert any(f["name"] == "note.txt" for f in body["files"])
        code, body = _request(url + "/admin/api/files", token=t2)
        assert body["files"] == []
        # X 的审计 Y 看不到
        code, body = _request(url + "/admin/api/audit", token=t2)
        assert body["audit"] == []
        assert tid1 != tid2
    finally:
        srv.shutdown()


def test_tenant_cannot_manage_tenants(monkeypatch, tmp_path):
    """租户 token 不能创建/停用租户（403）。"""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _start_server()
    try:
        code, body = _request(url + "/admin/api/tenants", method="POST", token="admin123",
                              payload={"name": "客户C"})
        t_token = body["token"]
        code, body = _request(url + "/admin/api/tenants", method="POST", token=t_token,
                              payload={"name": "越权"})
        assert code == 403
        code, body = _request(url + "/admin/api/tenants", token=t_token)
        assert code == 403
    finally:
        srv.shutdown()


def test_revoke_tenant_blocks_token(monkeypatch, tmp_path):
    """停用租户后，其 token 立即失效（401）。"""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _start_server()
    try:
        code, body = _request(url + "/admin/api/tenants", method="POST", token="admin123",
                              payload={"name": "客户D"})
        t_token, tid = body["token"], body["tenant_id"]
        code, body = _request(url + "/admin/api/tenants/" + tid + "/revoke",
                              method="POST", token="admin123")
        assert code == 200
        code, body = _request(url + "/run", method="POST", token=t_token,
                              payload={"query": "写文件"})
        assert code == 401
        # 管理员仍正常
        code, body = _request(url + "/admin/api/tenants", token="admin123")
        assert code == 200
        assert body["tenants"][0]["status"] == "inactive"
    finally:
        srv.shutdown()


def test_admin_token_still_works(monkeypatch, tmp_path):
    """管理员 token 行为向后兼容（原有全局工作区路径）。"""
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _start_server()
    try:
        code, body = _request(url + "/run", method="POST", token="admin123",
                              payload={"query": "写文件"})
        assert code == 200
        code, body = _request(url + "/admin/api/approvals/req-1/approve",
                              method="POST", token="admin123")
        assert code == 200
        assert (tmp_path / "ws" / "note.txt").read_text(encoding="utf-8") == "租户文件"
    finally:
        srv.shutdown()
