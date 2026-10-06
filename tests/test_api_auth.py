"""HTTP 服务鉴权测试：未配置 token 禁用 /run；配置后校验 Bearer/X-API-Token。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


def _start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(url: str, query: str = "hi", token: str | None = None, x_token: str | None = None):
    req = urllib.request.Request(
        url + "/run",
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if x_token:
        req.add_header("X-API-Token", x_token)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_run_disabled_without_token(monkeypatch):
    """未配置 ANENGOS_API_TOKEN：/run 拒绝服务，防止裸奔。"""
    monkeypatch.delenv("ANENGOS_API_TOKEN", raising=False)
    srv, url = _start_server()
    try:
        code, body = _post(url)
        assert code == 503
        assert "ANENGOS_API_TOKEN" in body["error"]
    finally:
        srv.shutdown()


def test_run_unauthorized_with_wrong_token(monkeypatch):
    """配置 token 后：缺失或错误的令牌一律 401。"""
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    srv, url = _start_server()
    try:
        code, _ = _post(url)  # 无 token
        assert code == 401
        code2, _ = _post(url, token="wrong-token")
        assert code2 == 401  # 错 token
    finally:
        srv.shutdown()


def test_run_authorized_passes_auth(monkeypatch):
    """正确令牌通过鉴权，进入下游（缺模型 key 返回 503，说明鉴权已过）。"""
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    monkeypatch.delenv("ANENGOS_API_KEY", raising=False)
    srv, url = _start_server()
    try:
        code, body = _post(url, token="secret123")
        assert code == 503
        assert "ANENGOS_API_KEY" in body["error"]
        code2, _ = _post(url, x_token="secret123")  # X-API-Token 头同样可用
        assert code2 == 503
    finally:
        srv.shutdown()


def test_health_needs_no_auth(monkeypatch):
    """健康检查不鉴权，并如实上报 auth_required。"""
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    srv, url = _start_server()
    try:
        with urllib.request.urlopen(url + "/health") as r:
            body = json.loads(r.read().decode("utf-8"))
        assert r.status == 200
        assert body["auth_required"] is True
    finally:
        srv.shutdown()
