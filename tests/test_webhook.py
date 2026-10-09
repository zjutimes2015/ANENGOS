"""Webhook 用量告警测试：阈值触发、同档位去重、禁用不触发、月度重置后可再触发。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app

from test_quota import _serve, _request


class _Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.__class__.received.append(json.loads(self.rfile.read(n).decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture()
def hook_server():
    _Hook.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/hook"
    srv.shutdown()


def test_webhook_fires_at_threshold_and_dedup(monkeypatch, tmp_path, hook_server):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "告警客户"}, method="POST")
        tid = d["tenant_id"]
        # 配置 webhook：阈值 0.5，启用
        code, r = _request(url + "/admin/api/settings/webhook",
                           {"url": hook_server, "threshold": 0.5, "enabled": True},
                           token="admin123")
        assert code == 200 and r["settings"]["url"] == hook_server
        # 配额 2：1 次 = 50% 触发 80 档，2 次 = 100% 触发 100 档
        _request(url + f"/admin/api/tenants/{tid}/quota", {"tasks_per_month": 2}, token="admin123")
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        # 第 3 次不触发新档位（同档位去重；但 100 档在 2/2 时已触发）
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        time.sleep(0.2)
        events = [p for p in _Hook.received if p["tenant_id"] == tid]
        levels = sorted(e["level"] for e in events)
        assert levels == ["100", "80"], levels
        # payload 字段完整
        e = events[0]
        assert e["event"] == "quota_alert" and e["name"] == "告警客户"
        assert e["tasks_used"] == 1 and e["tasks_quota"] == 2 and e["ratio"] == 0.5
    finally:
        srv.shutdown()


def test_webhook_disabled_no_fire(monkeypatch, tmp_path, hook_server):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "禁告警客户"}, method="POST")
        tid = d["tenant_id"]
        _request(url + "/admin/api/settings/webhook",
                 {"url": hook_server, "threshold": 0.5, "enabled": False}, token="admin123")
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        time.sleep(0.2)
        assert not [p for p in _Hook.received if p["tenant_id"] == tid]
        # 无 URL 时即使 enabled=True 也不发
        _request(url + "/admin/api/settings/webhook", {"url": "", "enabled": True}, token="admin123")
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        time.sleep(0.2)
        assert not [p for p in _Hook.received if p["tenant_id"] == tid]
    finally:
        srv.shutdown()


def test_webhook_config_validation_and_permission(monkeypatch, tmp_path, hook_server):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "配置校验客户"}, method="POST")
        tid, token = d["tenant_id"], d["token"]
        # 非法 URL
        code, r = _request(url + "/admin/api/settings/webhook", {"url": "ftp://x"}, token="admin123")
        assert code == 400
        # 非法阈值
        code, r = _request(url + "/admin/api/settings/webhook", {"url": hook_server, "threshold": 1.5}, token="admin123")
        assert code == 400
        # 非管理员 403
        code, r = _request(url + "/admin/api/settings/webhook", {"url": hook_server}, token=token)
        assert code == 403
    finally:
        srv.shutdown()


def test_webhook_rearm_after_monthly_reset(monkeypatch, tmp_path, hook_server):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "重置告警客户"}, method="POST")
        tid = d["tenant_id"]
        _request(url + "/admin/api/settings/webhook",
                 {"url": hook_server, "threshold": 0.5, "enabled": True}, token="admin123")
        _request(url + f"/admin/api/tenants/{tid}/quota", {"tasks_per_month": 2}, token="admin123")
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        time.sleep(0.2)
        assert len([p for p in _Hook.received if p["tenant_id"] == tid]) == 1
        # 跨月重置后应可再次触发
        app._TENANTS[tid]["usage"]["month"] = "2000-01"
        app._bump_usage({"role": "tenant", "tenant_id": tid})
        time.sleep(0.2)
        assert len([p for p in _Hook.received if p["tenant_id"] == tid]) == 2
    finally:
        srv.shutdown()
