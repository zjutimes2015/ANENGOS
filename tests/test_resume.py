"""总装线：任务挂起续跑（异步任务遇审批挂起，批准后自动续跑完成）。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


class _FakeLLM:
    """第一轮调 file_write（挂起），批准续跑后结束回合。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, schemas):
        self.calls += 1
        if self.calls == 1:
            return {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "r1", "name": "file_write",
                     "input": {"path": "resume.txt", "content": "续跑成功"}}
                ],
            }
        return {"stop_reason": "end_turn", "text": "任务完成：文件已写入并审计"}


def _request(url: str, method: str = "GET", token: str | None = None, payload=None):
    req = urllib.request.Request(url, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw.startswith("{") else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw.startswith("{") else raw


def _serve(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(app, "TENANTS_FILE", tmp_path / "tenants.json")
    html = tmp_path / "admin.html"
    html.write_text("<title>t</title>", encoding="utf-8")
    monkeypatch.setattr(app, "ADMIN_HTML", html)
    monkeypatch.setattr(app, "_APPROVALS", None)
    monkeypatch.setattr(app, "_TOOLS", None)
    monkeypatch.setattr(app, "_TENANTS", {})
    monkeypatch.setattr(app, "_TENANT_QUEUES", {})
    monkeypatch.setattr(app, "_TASKS", {})
    monkeypatch.setattr(app, "_REVIEWER", None)
    monkeypatch.setattr(app, "OpenAICompatLLM", _FakeLLM)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _wait_status(url, token, tid, *statuses, tries=60):
    for _ in range(tries):
        _, rec = _request(url + f"/admin/api/tasks/{tid}", token=token)
        if rec["status"] in statuses:
            return rec
        time.sleep(0.2)
    raise AssertionError(f"任务未达到 {statuses}：{rec}")


def test_async_task_pauses_and_resumes_after_approval(monkeypatch, tmp_path):
    """挂起续跑闭环：提交 -> waiting_approval -> 批准 -> 自动续跑 -> done + 文件落盘。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, body = _request(url + "/admin/api/run-async", "POST", "admin123",
                              {"query": "写入 resume.txt"})
        assert code == 202
        tid = body["task_id"]

        # 1. 任务挂起（不是完成）：等待审批
        rec = _wait_status(url, "admin123", tid, "waiting_approval")
        assert rec["pending_approvals"] == [{"id": "req-1", "tool": "file.write"}]
        assert rec["output"] is None
        assert not (tmp_path / "ws" / "resume.txt").exists()

        # 2. 批准 -> 自动续跑 -> 完成
        code, body = _request(url + "/admin/api/approvals/req-1/approve", "POST", "admin123")
        assert code == 200
        rec = _wait_status(url, "admin123", tid, "done")
        assert "任务完成" in rec["output"]
        assert rec["pending_approvals"] == []
        assert (tmp_path / "ws" / "resume.txt").read_text(encoding="utf-8") == "续跑成功"
    finally:
        srv.shutdown()


def test_async_task_rejected_pauses_resumes_with_rejection(monkeypatch, tmp_path):
    """拒绝后也续跑：模型收到拒绝结果继续推进。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, body = _request(url + "/admin/api/run-async", "POST", "admin123",
                              {"query": "写入 resume.txt"})
        tid = body["task_id"]
        _wait_status(url, "admin123", tid, "waiting_approval")

        code, body = _request(url + "/admin/api/approvals/req-1/reject", "POST", "admin123")
        assert code == 200
        rec = _wait_status(url, "admin123", tid, "done")
        assert "任务完成" in rec["output"]
        assert not (tmp_path / "ws" / "resume.txt").exists()  # 拒绝 -> 未落盘
    finally:
        srv.shutdown()


def test_reviews_endpoint_lists_verdicts(monkeypatch, tmp_path):
    """互审端点：展平审批应用事件中的 review 结论并返回最近结果。"""
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit.jsonl")
    from governance.audit import AuditLog
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.log({"actor": "a", "event": "approval_applied", "tool": "codex.submit", "args": {},
               "review": {"verdict": "pass", "score": 88, "reason": "ok"}})
    audit.log({"actor": "a", "event": "tool_call", "tool": "file.write", "args": {},
               "allowed": True, "decision": "x"})
    audit.log({"actor": "a", "event": "approval_applied", "tool": "grok.submit", "args": {},
               "review": {"verdict": "fix", "score": 30, "reason": "缺内容"}})
    monkeypatch.setattr(app, "_TENANTS", {})
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, body = _request(url + "/admin/api/reviews", token="admin123")
        assert code == 200
        assert len(body["reviews"]) == 2
        assert {r["verdict"] for r in body["reviews"]} == {"pass", "fix"}
    finally:
        srv.shutdown()
