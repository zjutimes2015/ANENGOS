"""配额与计费测试：租户任务计数、超配额 429、月度重置、管理员设配额、自助开通。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import app


def _serve(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(app, "BASE", tmp_path)
    monkeypatch.setattr(app, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(app, "ADMIN_HTML", tmp_path / "admin.html")
    monkeypatch.setattr(app, "TENANTS_FILE", tmp_path / "tenants.json")
    monkeypatch.setattr(app, "_TENANTS", {})
    monkeypatch.setattr(app, "_TENANT_QUEUES", {})
    monkeypatch.setattr(app, "_APPROVALS", None)
    monkeypatch.setattr(app, "_TOOLS", None)
    monkeypatch.setattr(app, "_TASKS", {})
    monkeypatch.setattr(app, "_REVIEWER", None)
    monkeypatch.setattr(app, "_SIGNUP_HITS", {})
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    monkeypatch.setenv("ANENGOS_API_KEY", "fake-key")
    monkeypatch.setenv("ANENGOS_BASE_URL", "http://127.0.0.1:1/v1")  # 不会被真正调用
    monkeypatch.setenv("ANENGOS_MODEL", "mock-model")
    (tmp_path / "admin.html").write_text("<html></html>", encoding="utf-8")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _request(url: str, data=None, token=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method or ("POST" if data is not None else "GET"))
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_tenant_created_with_default_quota(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "配额测试客户"}, method="POST")
        assert code == 201
        assert d["plan"] == "trial"
        assert d["quota"]["tasks_per_month"] == 100
        assert d["tenant_id"] in app._TENANTS
        assert app._TENANTS[d["tenant_id"]]["usage"]["tasks"] == 0
    finally:
        srv.shutdown()


def test_quota_enforced_and_usage_counts(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "配额耗尽客户"}, method="POST")
        tid, token = d["tenant_id"], d["token"]
        # 把配额压到 1
        code, _ = _request(url + f"/admin/api/tenants/{tid}/quota", {"tasks_per_month": 1}, token="admin123")
        assert code == 200
        # 第一次任务：通过（mock 模型返回错误 -> 503？用无 key 场景：模型不可用会 503，但计数仍 +1）
        # 用真实 run 需模型；这里只验证配额逻辑，绕过 agent 执行：直接调 app._bump_usage
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        assert app._tenant_record(tid)["usage"]["tasks"] == 1
        # 第二次应超配额（通过 usage 端点确认配额状态 + 直接校验函数）
        assert app._quota_error({"role": "tenant", "tenant_id": tid}) is not None
        # run-async 端点应返回 429
        code, d = _request(url + "/admin/api/run-async", {"query": "写个文件"}, token=token)
        assert code == 429
        assert "配额" in d["error"]
    finally:
        srv.shutdown()


def test_quota_monthly_reset(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "月度重置客户"}, method="POST")
        tid = d["tenant_id"]
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        assert app._tenant_record(tid)["usage"]["tasks"] == 1
        # 模拟跨月：把 usage.month 改为旧月份
        import datetime
        old = "2000-01"
        app._TENANTS[tid]["usage"]["month"] = old
        assert app._tenant_record(tid)["usage"]["tasks"] == 0  # 自动重置
        assert app._tenant_record(tid)["usage"]["month"] != old
        assert app._quota_error({"role": "tenant", "tenant_id": tid}) is None
    finally:
        srv.shutdown()


def test_usage_endpoint_admin_and_tenant(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "用量查询客户"}, method="POST")
        tid, token = d["tenant_id"], d["token"]
        code, d = _request(url + "/admin/api/usage", token="admin123")
        assert code == 200
        row = [u for u in d["usage"] if u["tenant_id"] == tid][0]
        assert row["tasks_used"] == 0 and row["tasks_quota"] == 100
        code, d = _request(url + "/admin/api/usage", token=token)
        assert code == 200
        assert d["usage"]["tenant_id"] == tid
        assert d["usage"]["tasks_quota"] == 100
    finally:
        srv.shutdown()


def test_signup_rate_limit(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(app, "_SIGNUP_LIMIT", 2)
        for i in range(2):
            code, _ = _request(url + "/api/signup", {"name": f"限流客户{i}"}, method="POST")
            assert code == 201
        code, d = _request(url + "/api/signup", {"name": "第三个"}, method="POST")
        assert code == 429
        assert "开通" in d["error"]
    finally:
        srv.shutdown()


def test_admin_can_set_invalid_quota_rejected(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "非法配额客户"}, method="POST")
        tid, token = d["tenant_id"], d["token"]
        code, d = _request(url + f"/admin/api/tenants/{tid}/quota", {"tasks_per_month": "abc"}, token="admin123")
        assert code == 400
        # 非管理员禁止
        code, d = _request(url + f"/admin/api/tenants/{tid}/quota", {"tasks_per_month": 5}, token=token)
        assert code == 403
    finally:
        srv.shutdown()
