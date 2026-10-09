"""微信支付官方通道：Native 下单（JSAPI 二维码 code_url）+ APIv3 签名 + 查单 + 回调验签解密。

环境变量：
    WECHAT_APPID                公众号/小程序 AppID
    WECHAT_MCH_ID                商户号
    WECHAT_SERIAL_NO             商户 API 证书序列号
    WECHAT_PRIVATE_KEY           商户 API 私钥（PEM，可含头尾或单行 base64）
    WECHAT_PLATFORM_PUBLIC_KEY   微信支付平台证书公钥（PEM，用于回调验签；单行 base64 亦可）
    WECHAT_API_V3_KEY            APIv3 密钥（32 字节，用于回调 AES-GCM 解密）
    WECHAT_NOTIFY_URL            支付结果回调地址（需公网可达；未备案阶段可留空，依赖主动查单）

金额口径：订单 amount_cents 为分，微信接口 total 直接用分（整数）。
"""
import base64
import json
import os
import time
import urllib.request

BASE = "https://api.mch.weixin.qq.com"


def _pem_load(pem_or_b64: str, kind: str) -> bytes:
    s = pem_or_b64.strip()
    if "-----BEGIN" not in s:
        tag = "PRIVATE KEY" if kind == "private" else "PUBLIC KEY"
        s = f"-----BEGIN {tag}-----\n" + s + f"\n-----END {tag}-----"
    return s.encode()


def _sign_request(method: str, url_path: str, body: str) -> str:
    """生成 APIv3 Authorization 头。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key = serialization.load_pem_private_key(
        _pem_load(os.getenv("WECHAT_PRIVATE_KEY", ""), "private"), password=None)
    timestamp = str(int(time.time()))
    nonce = base64.b64encode(os.urandom(16)).decode()
    message = f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"
    signature = base64.b64encode(
        private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode()
    return (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{os.getenv("WECHAT_MCH_ID", "")}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{os.getenv("WECHAT_SERIAL_NO", "")}"'
    )


def _request(method: str, url_path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    req = urllib.request.Request(BASE + url_path, data=data or None, method=method)
    req.add_header("Authorization", _sign_request(method, url_path, (data or b"").decode()))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_payment(order: dict) -> dict:
    """Native 下单：返回 code_url（扫码支付链接）。"""
    body = {
        "appid": os.getenv("WECHAT_APPID", ""),
        "mchid": os.getenv("WECHAT_MCH_ID", ""),
        "description": f"ANENGOS {order.get('plan_name', '订阅')}",
        "out_trade_no": order["order_id"],
        "notify_url": os.getenv("WECHAT_NOTIFY_URL", ""),
        "amount": {"total": order["amount_cents"], "currency": "CNY"},
    }
    resp = _request("POST", "/v3/pay/transactions/native", body)
    if "code_url" not in resp:
        raise RuntimeError("微信下单失败：" + json.dumps(resp, ensure_ascii=False)[:500])
    return {"code_url": resp["code_url"], "provider": "wechat"}


def query_payment(order: dict) -> str:
    """主动查单：trade_state == SUCCESS -> paid。"""
    url_path = f"/v3/pay/transactions/out-trade-no/{order['order_id']}?mchid={os.getenv('WECHAT_MCH_ID', '')}"
    try:
        resp = _request("GET", url_path)
        if resp.get("trade_state") == "SUCCESS":
            return "paid"
        return "pending"
    except Exception:
        return "pending"


def _verify_notify(serial_no: str, timestamp: str, nonce: str, signature_b64: str, body: str) -> bool:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        public_key = serialization.load_pem_public_key(
            _pem_load(os.getenv("WECHAT_PLATFORM_PUBLIC_KEY", ""), "public"))
        message = f"{timestamp}\n{nonce}\n{body}\n"
        public_key.verify(base64.b64decode(signature_b64), message.encode("utf-8"),
                          padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def _decrypt_resource(ciphertext_b64: str, nonce: str, associated_data: str) -> dict:
    """APIv3 回调 resource AES-256-GCM 解密。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.getenv("WECHAT_API_V3_KEY", "").encode("utf-8")
    aes = AESGCM(key)
    plain = aes.decrypt(nonce.encode("utf-8"), base64.b64decode(ciphertext_b64), associated_data.encode("utf-8"))
    return json.loads(plain.decode("utf-8"))


def handle_notify(raw_body: bytes, headers: dict) -> tuple[int, dict]:
    """微信支付回调：验签 + AES-GCM 解密，返回 (200, {order_id, settle})。"""
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return 400, {"error": "无效回调体"}
    h = {k.lower(): v for k, v in headers.items()}
    if not _verify_notify(h.get("wechatpay-serial", ""),
                          h.get("wechatpay-timestamp", ""),
                          h.get("wechatpay-nonce", ""),
                          h.get("wechatpay-signature", ""),
                          raw_body.decode("utf-8")):
        return 400, {"error": "签名校验失败"}
    try:
        resource = data.get("resource", {})
        dec = _decrypt_resource(resource.get("ciphertext", ""),
                                resource.get("nonce", ""),
                                resource.get("associated_data", ""))
    except Exception:
        return 400, {"error": "回调解密失败"}
    settle = dec.get("trade_state") == "SUCCESS"
    return 200, {"order_id": dec.get("out_trade_no"), "settle": settle}
