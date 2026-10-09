"""总装线：异步任务队列（run-async 不阻塞 HTTP，任务可查询、租户隔离）。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


class _FakeLLM:
    """直接结束回合，任务快速完成。"""

    def __call__(self, messages, schemas):
        return {"stop_reason": "end_turn", "text": "异步任务完成"}


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


def test_async_task_lifecycle(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        # 提交立即返回，不等任务完成
        code, body = _request(url + "/admin/api/run-async", "POST", "admin123", {"query": "生成周报"})
        assert code == 202
        tid = body["task_id"]
        assert body["status"] == "running"

        # 轮询直到完成
        rec = None
        for _ in range(50):
            code, rec = _request(url + f"/admin/api/tasks/{tid}", token="admin123")
            assert code == 200
            if rec["status"] in ("done", "error"):
                break
            time.sleep(0.2)
        assert rec["status"] == "done"
        assert "异步任务完成" in rec["output"]
        assert rec["finished_at"]

        # 任务列表可见
        code, body = _request(url + "/admin/api/tasks", token="admin123")
        assert any(t["id"] == tid for t in body["tasks"])
    finally:
        srv.shutdown()


def test_async_task_unknown_404(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, body = _request(url + "/admin/api/tasks/nope", token="admin123")
        assert code == 404
    finally:
        srv.shutdown()
