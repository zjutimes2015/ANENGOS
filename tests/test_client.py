"""客户站测试：token 登录（验证码/限流）-> 会话 Cookie -> 问答 UI 链路。"""
import base64
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import app

from test_auth import _setup
from test_knowledge import _mk_tenant


def _copy_pages(tmp_path):
    src = Path(app.__file__).parent
    for f in ("client_login.html", "client.html", "login.html", "signup.html", "index.html", "pay.html"):
        shutil.copy(src / f, tmp_path / f)


def _req_raw(url, data=None, method=None, cookie=None, token=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                               headers=headers, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def _get_captcha_answer(url):
    """从内存验证码表读取答案（测试进程共享 app 全局）。"""
    import app
    _, _, body = _req_raw(url + "/api/captcha", method="GET")
    d = json.loads(body)
    return d["captcha_id"], app._CAPTCHAS[d["captcha_id"]]["answer"]


def test_client_page_redirects_when_anon(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    _copy_pages(tmp_path)
    try:
        code, _, body = _req_raw(url + "/client", method="GET")
        assert code == 200
        assert "客户门户" in body and "访问令牌" in body, "匿名应看到登录页"
        code, _, body = _req_raw(url + "/api/client/session", method="GET")
        assert json.loads(body)["ok"] is False
    finally:
        srv.shutdown()


def test_tenant_login_session_and_ask(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    _copy_pages(tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 上传资料（租户 token）
        code, _, body = _req_raw(url + "/api/tenant/knowledge/upload",
                                 {"filename": "客户资料.txt",
                                  "content": base64.b64encode("我们公司采购企业版，年付享 9 折，预算 20 万。".encode()).decode(),
                                  "tags": []}, method="POST", token=token)
        assert code in (200, 201)
        # 验证码 + token 登录
        cid, ans = _get_captcha_answer(url)
        code, headers, body = _req_raw(url + "/api/client/login",
                                       {"token": token, "captcha_id": cid, "captcha_answer": ans}, method="POST")
        assert code == 200, body
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        assert cookie.startswith("anengos_session=")
        # 会话态：session ok + /client 返回问答页
        code, _, body = _req_raw(url + "/api/client/session", method="GET", cookie=cookie)
        assert json.loads(body)["ok"] is True
        code, _, body = _req_raw(url + "/client", method="GET", cookie=cookie)
        assert "AI 问答" in body or "提问" in body
        # 会话 cookie 可调租户知识库 API
        code, _, body = _req_raw(url + "/api/tenant/knowledge/ask",
                                 {"query": "年付折扣"}, method="POST", cookie=cookie)
        assert code == 200
        assert json.loads(body)["sources"]
        # 退出后会话失效
        code, _, body = _req_raw(url + "/api/client/logout", method="POST", cookie=cookie)
        assert json.loads(body)["ok"] is True
        code, _, body = _req_raw(url + "/api/client/session", method="GET", cookie=cookie)
        assert json.loads(body)["ok"] is False
    finally:
        srv.shutdown()


def test_client_login_bad_token_and_captcha(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    _copy_pages(tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 错误令牌 -> 401
        cid, ans = _get_captcha_answer(url)
        code, _, body = _req_raw(url + "/api/client/login",
                                 {"token": "bad-token", "captcha_id": cid, "captcha_answer": ans}, method="POST")
        assert code == 401
        # 验证码错误 -> 400（即使令牌正确）
        cid, ans = _get_captcha_answer(url)
        code, _, body = _req_raw(url + "/api/client/login",
                                 {"token": token, "captcha_id": cid, "captcha_answer": str(int(ans) + 1)}, method="POST")
        assert code == 400
        # 管理员令牌不能登录客户站 -> 401
        cid, ans = _get_captcha_answer(url)
        code, _, body = _req_raw(url + "/api/client/login",
                                 {"token": "admin123", "captcha_id": cid, "captcha_answer": ans}, method="POST")
        assert code == 401, "管理员令牌不应能登录客户站"
        # 缺少令牌 -> 400
        cid, ans = _get_captcha_answer(url)
        code, _, body = _req_raw(url + "/api/client/login",
                                 {"token": "", "captcha_id": cid, "captcha_answer": ans}, method="POST")
        assert code == 400
    finally:
        srv.shutdown()
