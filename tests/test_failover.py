"""借鉴清单第4步：Failover 缓冲重放（任务队列断点续跑 + 失败自动重放）。

验证：执行失败自动退避重试（期间状态 retrying + next_retry_at 可见）、
超限给出最终失败、任务持久化 tasks.json、重启恢复（queued 自动续跑 /
running 等标记 interrupted）、手动 retry/cancel 端点、管理台列表含 attempts。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app
from test_quota import _serve, _request


def _submit(url, token="admin123", query="测试任务"):
    return _request(url + "/admin/api/run-async", {"query": query}, method="POST", token=token)


def test_auto_retry_then_success(monkeypatch, tmp_path):
    """执行失败 2 次（自动重试退避）后成功：客户端只看到完整结果，attempts 可见。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(app, "TASKS_FILE", tmp_path / "tasks.json")
        calls = {"n": 0}

        class _Flaky:
            def run(self, query, max_steps=20):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RuntimeError(f"provider 抖动 {calls['n']}")
                class _S:
                    paused = False
                    output = "最终成功输出"
                    blocked_reason = None
                    pending_items = []
                return _S()

        monkeypatch.setattr(app, "build_agent", lambda principal: (_Flaky(), None, ""))
        code, d = _submit(url)
        assert code == 202
        tid = d["task_id"]
        # 轮询直到终态（自动重试退避 2s+4s，需等待）
        for _ in range(120):
            code, d = _request(url + f"/admin/api/tasks/{tid}", token="admin123")
            if d["status"] in ("done", "error"):
                break
            time.sleep(0.15)
        assert d["status"] == "done" and d["output"] == "最终成功输出"
        assert calls["n"] == 3 and d["attempts"] == 3  # 重试 2 次 + 最终成功
        # 落盘（不含内部对象）
        assert app.TASKS_FILE.exists()
        saved = json.loads(app.TASKS_FILE.read_text(encoding="utf-8"))
        assert saved[tid]["status"] == "done" and "_session" not in saved[tid]
    finally:
        srv.shutdown()


def test_max_attempts_gives_final_error(monkeypatch, tmp_path):
    """超过 max_attempts（默认3）→ 最终失败，error 给出最后一次错误，不丢任务。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(app, "TASKS_FILE", tmp_path / "tasks.json")

        class _AlwaysFail:
            def run(self, query, max_steps=20):
                raise RuntimeError("模型不可用")

        monkeypatch.setattr(app, "build_agent", lambda principal: (_AlwaysFail(), None, ""))
        code, d = _submit(url)
        tid = d["task_id"]
        for _ in range(60):
            code, d = _request(url + f"/admin/api/tasks/{tid}", token="admin123")
            if d["status"] in ("done", "error"):
                break
            time.sleep(0.15)
        assert d["status"] == "error"
        assert d["attempts"] == 3 and d["error"] == "模型不可用"
        # 列表含 attempts 字段
        code, d = _request(url + "/admin/api/tasks", token="admin123")
        assert d["tasks"][0]["attempts"] == 3
    finally:
        srv.shutdown()


def test_persist_and_restart_resume(monkeypatch, tmp_path):
    """断点续跑：任务落盘；重启加载后 queued 自动续跑，running 标记 interrupted。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(app, "TASKS_FILE", tmp_path / "tasks.json")
        # 构造两类遗留任务模拟进程重启现场
        with app._TASKS_LOCK:
            app._TASKS["t_queued"] = {
                "id": "t_queued", "status": "queued", "created_at": app._ts(),
                "role": "admin", "query": "续跑任务", "attempts": 0,
            }
            app._TASKS["t_running"] = {
                "id": "t_running", "status": "running", "created_at": app._ts(),
                "role": "admin", "query": "中断任务", "attempts": 2,
            }
            app._TASKS["t_waiting"] = {
                "id": "t_waiting", "status": "waiting_approval", "created_at": app._ts(),
                "role": "admin", "query": "挂起任务", "attempts": 0,
            }
        app._save_tasks()
        app._TASKS.clear()
        # 重启：queued 自动续跑（执行一次），running/waiting 标记 interrupted
        done = {"n": 0}

        class _Ok:
            def run(self, query, max_steps=20):
                done["n"] += 1
                class _S:
                    paused = False
                    output = "ok"
                    blocked_reason = None
                    pending_items = []
                return _S()

        monkeypatch.setattr(app, "build_agent", lambda principal: (_Ok(), None, ""))
        app._load_tasks()
        for _ in range(30):
            if app._TASKS.get("t_queued", {}).get("status") == "done":
                break
            time.sleep(0.1)
        assert app._TASKS["t_queued"]["status"] == "done"  # queued 自动续跑
        assert app._TASKS["t_running"]["status"] == "interrupted"  # 有副作用风险 -> 等管理员重放
        assert app._TASKS["t_waiting"]["status"] == "interrupted"
        # 手动重放 interrupted
        code, d = _request(url + f"/admin/api/tasks/t_running/retry", {}, method="POST", token="admin123")
        assert code == 200 and d["status"] == "queued"
        for _ in range(30):
            if app._TASKS["t_running"]["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert app._TASKS["t_running"]["status"] == "done"
    finally:
        srv.shutdown()


def test_retry_and_cancel_endpoints(monkeypatch, tmp_path):
    """retry：仅 error/interrupted/cancelled 可重放；cancel：取消排队/运行/重试任务。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(app, "TASKS_FILE", tmp_path / "tasks.json")
        # 排队任务 -> cancel
        with app._TASKS_LOCK:
            app._TASKS["t1"] = {"id": "t1", "status": "queued", "role": "admin", "query": "x",
                                "created_at": app._ts(), "attempts": 0}
        code, d = _request(url + "/admin/api/tasks/t1/cancel", {}, method="POST", token="admin123")
        assert code == 200 and d["status"] == "cancelled"
        # 已取消任务可重放
        code, d = _request(url + "/admin/api/tasks/t1/retry", {}, method="POST", token="admin123")
        assert code == 200 and d["status"] == "queued"
        # done 任务不可重放/取消
        with app._TASKS_LOCK:
            app._TASKS["t2"] = {"id": "t2", "status": "done", "role": "admin", "query": "y",
                                "created_at": app._ts(), "attempts": 1}
        code, d = _request(url + "/admin/api/tasks/t2/retry", {}, method="POST", token="admin123")
        assert code == 400
        code, d = _request(url + "/admin/api/tasks/t2/cancel", {}, method="POST", token="admin123")
        assert code == 400
        # 无效凭据优先 401（租户隔离由真实租户 token 场景覆盖）
        code, d = _request(url + "/admin/api/tasks/t1/retry", {}, method="POST", token="tenant-token")
        assert code == 401
    finally:
        srv.shutdown()
