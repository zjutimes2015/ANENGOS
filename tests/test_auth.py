"""登录与人机验证测试：验证码生成/校验、登录成功/失败/锁定、会话 Cookie 鉴权、signup 账号、登出。"""
import json
import os

import app

from test_quota import _serve, _request


def _setup(monkeypatch, tmp_path, admin_user="admin", admin_pass="secret-pass-123"):
    from test_api_admin import _reset_state
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "secret123")
    monkeypatch.setenv("ANENGOS_ADMIN_USER", admin_user)
    monkeypatch.setenv("ANENGOS_ADMIN_PASS", admin_pass)
    monkeypatch.setattr(app, "_ACCOUNTS_FILE", tmp_path / "accounts.json")
    app._load_accounts()
    app._ensure_admin()
    app._SESSIONS.clear()
    app._CAPTCHAS.clear()
    app._LOGIN_FAILS.clear()
    return _serve(monkeypatch, tmp_path)


def _get_captcha(url):
    import urllib.request
    with urllib.request.urlopen(url + "/api/captcha", timeout=10) as r:
        return json.loads(r.read().decode())


def _login(url, username, password, captcha_id, captcha_answer):
    return _request(url + "/api/login",
                    {"username": username, "password": password,
                     "captcha_id": captcha_id, "captcha_answer": captcha_answer},
                    method="POST")


def test_captcha_generate_and_verify(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        d = _get_captcha(url)
        assert d["captcha_id"] and d["image"].startswith("data:image/png;base64,")
        # 内部直接校验（答案在内存）
        c = app._CAPTCHAS.get(d["captcha_id"])
        assert c is not None
        assert app._check_captcha(d["captcha_id"], c["answer"]) is True
        # 已消费，二次校验失败
        assert app._check_captcha(d["captcha_id"], c["answer"]) is False
        # 不存在/过期
        assert app._check_captcha("nope", "1") is False
    finally:
        srv.shutdown()


def test_login_success_session_and_me(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        c = _get_captcha(url)
        app._CAPTCHAS[c["captcha_id"]] = {"answer": "42", "exp": 10**12}
        code, d = _login(url, "admin", "secret-pass-123", c["captcha_id"], "42")
        assert code == 200 and d["role"] == "admin"
        # 会话 Cookie 鉴权访问管理接口
        sid = app._SESSIONS
        assert len(sid) == 1
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", url.split(":")[-1] and int(url.split(":")[-1].rstrip("/")), timeout=10)
        conn.request("GET", "/admin/api/me", headers={"Cookie": "anengos_session=" + next(iter(sid))})
        r = conn.getresponse(); body = json.loads(r.read().decode())
        assert r.status == 200 and body["role"] == "admin"
        conn.close()
    finally:
        srv.shutdown()


def test_login_wrong_captcha_and_wrong_password(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        c = _get_captcha(url)
        code, d = _login(url, "admin", "secret-pass-123", c["captcha_id"], "WRONG")
        assert code == 400 and "验证码" in d["error"]
        c = _get_captcha(url)
        app._CAPTCHAS[c["captcha_id"]] = {"answer": "7", "exp": 10**12}
        code, d = _login(url, "admin", "bad-password", c["captcha_id"], "7")
        assert code == 401 and "用户名或密码错误" in d["error"]
    finally:
        srv.shutdown()


def test_login_lock_after_failures(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        for i in range(5):
            c = _get_captcha(url)
            app._CAPTCHAS[c["captcha_id"]] = {"answer": "7", "exp": 10**12}
            code, d = _login(url, "admin", "bad", c["captcha_id"], "7")
            assert code == 401
        # 第 6 次被锁
        c = _get_captcha(url)
        app._CAPTCHAS[c["captcha_id"]] = {"answer": "7", "exp": 10**12}
        code, d = _login(url, "admin", "secret-pass-123", c["captcha_id"], "7")
        assert code == 429 and "失败次数过多" in d["error"]
    finally:
        srv.shutdown()


def test_signup_with_account_and_web_login(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        c = _get_captcha(url)
        app._CAPTCHAS[c["captcha_id"]] = {"answer": "9", "exp": 10**12}
        code, d = _request(url + "/api/signup",
                           {"name": "账号客户", "username": "client1", "password": "passw0rd!",
                            "captcha_id": c["captcha_id"], "captcha_answer": "9"},
                           method="POST")
        assert code == 201 and d["account_created"] is True
        # 用新账号登录
        c2 = _get_captcha(url)
        app._CAPTCHAS[c2["captcha_id"]] = {"answer": "11", "exp": 10**12}
        code, d2 = _login(url, "client1", "passw0rd!", c2["captcha_id"], "11")
        assert code == 200 and d2["role"] == "tenant"
        # signup 验证码错误被拒
        code, d3 = _request(url + "/api/signup", {"name": "X", "captcha_id": "bad", "captcha_answer": "1"}, method="POST")
        assert code == 400 and "验证码" in d3["error"]
    finally:
        srv.shutdown()


def test_logout_destroys_session(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        c = _get_captcha(url)
        app._CAPTCHAS[c["captcha_id"]] = {"answer": "42", "exp": 10**12}
        code, d = _login(url, "admin", "secret-pass-123", c["captcha_id"], "42")
        assert code == 200
        assert len(app._SESSIONS) == 1
        sid = next(iter(app._SESSIONS))
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", int(url.split(":")[-1].rstrip("/")), timeout=10)
        conn.request("POST", "/api/logout", headers={"Cookie": "anengos_session=" + sid})
        r = conn.getresponse(); r.read()
        assert r.status == 200
        assert sid not in app._SESSIONS
        conn.close()
    finally:
        srv.shutdown()
