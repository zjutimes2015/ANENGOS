"""支付适配器单元测试：mock 通道回归 + 支付宝/微信验签逻辑（用测试密钥，不依赖真实商户凭据）。

覆盖：
  - _provider 工厂按名加载（mock/alipay/wechat）
  - 支付宝 RSA2 签名与验签（自签自验：签名->剔除 sign 后验签）
  - 支付宝回调验签通过/失败/AppID 不匹配
  - 微信 APIv3 回调验签（测试公私钥）+ AES-GCM 解密
  - mock 下单与回调结算全链路（含真实通道下单失败时返回 502）
"""
import base64
import json
import os
import urllib.parse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import app
from billing_providers import alipay, wechat


@pytest.fixture(scope="module")
def rsa_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def test_provider_factory():
    def short(name):
        return app._provider(name).__name__.split(".")[-1]
    assert short("mock") == "mock"
    assert short("alipay") == "alipay"
    assert short("wechat") == "wechat"
    assert short("nope") == "mock"  # 未知通道回退 mock


def test_alipay_sign_verify(rsa_pair, monkeypatch):
    priv, pub = rsa_pair
    monkeypatch.setenv("ALIPAY_PRIVATE_KEY", priv)
    monkeypatch.setenv("ALIPAY_PUBLIC_KEY", pub)
    params = {"app_id": "2026TEST", "method": "alipay.trade.precreate",
              "biz_content": json.dumps({"out_trade_no": "od_t1", "total_amount": "299.00"})}
    params["sign"] = alipay._sign(params)
    content = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "sign")
    assert alipay._verify(content, params["sign"]) is True
    # 篡改后验签失败
    assert alipay._verify(content + "x", params["sign"]) is False


def test_alipay_notify_verify(rsa_pair, monkeypatch):
    priv, pub = rsa_pair
    monkeypatch.setenv("ALIPAY_PRIVATE_KEY", priv)
    monkeypatch.setenv("ALIPAY_PUBLIC_KEY", pub)
    monkeypatch.setenv("ALIPAY_APP_ID", "2026TEST")
    form = {"app_id": "2026TEST", "out_trade_no": "od_t1", "trade_status": "TRADE_SUCCESS",
            "total_amount": "299.00", "sign_type": "RSA2"}
    content = "&".join(f"{k}={form[k]}" for k in sorted(form))
    form["sign"] = alipay._sign(form)
    # 真实回调为 urlencoded 表单：+ 号必须编码为 %2B，parse_qs 才能还原
    body = urllib.parse.urlencode(form).encode()
    code, info = alipay.handle_notify(body, {})
    assert code == 200 and info["order_id"] == "od_t1" and info["settle"] is True
    # 签名错误 -> 400
    bad_form = dict(form)
    bad_form["sign"] = "A" * len(form["sign"])
    bad = urllib.parse.urlencode(bad_form).encode()
    assert alipay.handle_notify(bad, {})[0] == 400
    # AppID 不匹配 -> 400
    monkeypatch.setenv("ALIPAY_APP_ID", "OTHER_APP")
    assert alipay.handle_notify(body, {})[0] == 400


def test_wechat_notify_verify_and_decrypt(rsa_pair, monkeypatch):
    priv, pub = rsa_pair
    monkeypatch.setenv("WECHAT_PRIVATE_KEY", priv)
    monkeypatch.setenv("WECHAT_PLATFORM_PUBLIC_KEY", pub)
    monkeypatch.setenv("WECHAT_API_V3_KEY", "0123456789abcdef0123456789abcdef")

    # 构造一个"微信支付平台"发来的回调：用平台私钥签名（此处用同一对测试密钥模拟）
    resource = {"out_trade_no": "od_w1", "trade_state": "SUCCESS", "mchid": "1900000001"}
    aes_key = b"0123456789abcdef0123456789abcdef"
    nonce = "abcdefghijklmnop"  # 16 字节随机串（微信回调规范）
    aad = "transaction"
    ct = AESGCM(aes_key).encrypt(nonce.encode(), json.dumps(resource).encode(), aad.encode())
    payload = {"resource": {"ciphertext": base64.b64encode(ct).decode(), "nonce": nonce,
                            "associated_data": aad}}
    raw = json.dumps(payload).encode()
    # 平台签名
    from cryptography.hazmat.primitives.asymmetric import padding as _padding
    priv_key = serialization.load_pem_private_key(priv.encode(), password=None)
    sig = base64.b64encode(priv_key.sign(
        ("1700000000\nnonce123\n" + raw.decode() + "\n").encode(), _padding.PKCS1v15(), hashes.SHA256())).decode()
    headers = {"wechatpay-serial": "SERIAL", "wechatpay-timestamp": "1700000000",
               "wechatpay-nonce": "nonce123", "wechatpay-signature": sig}
    code, info = wechat.handle_notify(raw, headers)
    assert code == 200 and info["order_id"] == "od_w1" and info["settle"] is True
    # 签名错误 -> 400
    assert wechat.handle_notify(raw, {**headers, "wechatpay-signature": "AAAA"})[0] == 400


def test_mock_order_full_flow(monkeypatch, tmp_path):
    from test_quota import _serve, _request
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, s = _request(url + "/api/signup", {"name": "适配器测试客户"}, method="POST")
        token = s["token"]
        code, o = _request(url + "/api/billing/order", {"plan": "team", "tenant_id": token}, method="POST")
        assert code == 200 and o["provider"] == "mock" and o.get("mock") is True
        # mock 订单无支付参数，收银台走模拟按钮
        code, d = _request(url + "/api/billing/order?id=" + o["order_id"])
        assert d["mock"] is True and d["qrcode"] == ""
    finally:
        srv.shutdown()


def test_real_provider_order_fails_gracefully(monkeypatch, tmp_path):
    """真实通道缺少密钥时下单返回 502 而非崩溃。"""
    from test_quota import _serve, _request
    monkeypatch.delenv("ALIPAY_APP_ID", raising=False)
    monkeypatch.delenv("ALIPAY_PRIVATE_KEY", raising=False)
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, s = _request(url + "/api/signup", {"name": "通道测试客户"}, method="POST")
        token = s["token"]
        code, o = _request(url + "/api/billing/order",
                           {"plan": "team", "tenant_id": token, "provider": "alipay"}, method="POST")
        assert code == 502 and "支付通道下单失败" in o["error"]
    finally:
        srv.shutdown()
