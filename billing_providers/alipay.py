"""支付宝官方通道：当面付（alipay.trade.precreate）+ RSA2 签名 + 主动查单/回调验签。

环境变量：
    ALIPAY_APP_ID          应用 AppID
    ALIPAY_PRIVATE_KEY     应用私钥（PEM 单行，去掉头和换行的 base64 亦可）
    ALIPAY_PUBLIC_KEY      支付宝公钥（PEM 单行或标准 PEM）
    ALIPAY_GATEWAY         网关（默认正式 https://openapi.alipay.com/gateway.do；沙箱可覆盖）

金额口径：本系统订单 amount_cents 为分；支付宝接口 total_amount 为元字符串（两位小数）。
"""
import base64
import json
import os
import time
import urllib.parse
import urllib.request

GATEWAY = os.getenv("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do")


def _app_id() -> str:
    return os.getenv("ALIPAY_APP_ID", "")


def _pem_load(pem_or_b64: str, kind: str) -> bytes:
    """加载 PEM 私钥/公钥（兼容单行 base64 与标准 PEM）。"""
    s = pem_or_b64.strip()
    if "-----BEGIN" not in s:
        if kind == "private":
            s = "-----BEGIN PRIVATE KEY-----\n" + s + "\n-----END PRIVATE KEY-----"
        else:
            s = "-----BEGIN PUBLIC KEY-----\n" + s + "\n-----END PUBLIC KEY-----"
    return s.encode()


def _sign(params: dict) -> str:
    """RSA2 签名：剔除 sign/sign_type，ASCII 排序后拼接 key=value&...，SHA256withRSA。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key = serialization.load_pem_private_key(
        _pem_load(os.getenv("ALIPAY_PRIVATE_KEY", ""), "private"), password=None)
    content = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sig = private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def _verify(content: str, signature_b64: str) -> bool:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        public_key = serialization.load_pem_public_key(
            _pem_load(os.getenv("ALIPAY_PUBLIC_KEY", ""), "public"))
        public_key.verify(base64.b64decode(signature_b64), content.encode("utf-8"),
                          padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def _post(params: dict) -> dict:
    """调用支付宝网关（application/x-www-form-urlencoded），返回 JSON。"""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(GATEWAY, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_payment(order: dict) -> dict:
    """当面付预下单：返回收款二维码内容（qr_code）。"""
    biz = {
        "out_trade_no": order["order_id"],
        "total_amount": f"{order['amount_cents'] / 100:.2f}",
        "subject": f"ANENGOS {order.get('plan_name', '订阅')}",
        "product_code": "FACE_TO_FACE_PAYMENT",
    }
    params = {
        "app_id": _app_id(),
        "method": "alipay.trade.precreate",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz, ensure_ascii=False),
    }
    params["sign"] = _sign(params)
    resp = _post(params)
    if "alipay_trade_precreate_response" not in resp:
        raise RuntimeError("支付宝下单失败：" + json.dumps(resp, ensure_ascii=False)[:500])
    r = resp["alipay_trade_precreate_response"]
    if r.get("code") != "10000":
        raise RuntimeError(f"支付宝下单失败 [{r.get('code')}] {r.get('sub_msg', r.get('msg'))}")
    return {"qrcode": r.get("qr_code", ""), "provider": "alipay"}


def query_payment(order: dict) -> str:
    """主动查单：TRADE_SUCCESS / TRADE_FINISHED -> paid。"""
    biz = {"out_trade_no": order["order_id"]}
    params = {
        "app_id": _app_id(),
        "method": "alipay.trade.query",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz, ensure_ascii=False),
    }
    params["sign"] = _sign(params)
    try:
        resp = _post(params)
        r = resp.get("alipay_trade_query_response", {})
        if r.get("code") == "10000" and r.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            return "paid"
        return "pending"
    except Exception:
        return "pending"


def handle_notify(raw_body: bytes, headers: dict) -> tuple[int, dict]:
    """支付宝异步回调验签（form 表单）。验证通过返回 (200, {order_id, settle})。"""
    try:
        data = urllib.parse.parse_qs(raw_body.decode("utf-8"))
        flat = {k: v[0] for k, v in data.items()}
    except Exception:
        return 400, {"error": "无效回调体"}
    sign = flat.pop("sign", "")
    if not sign or not flat:
        return 400, {"error": "缺少签名"}
    content = "&".join(f"{k}={flat[k]}" for k in sorted(flat))
    if not _verify(content, sign):
        return 400, {"error": "签名校验失败"}
    if flat.get("app_id") != _app_id():
        return 400, {"error": "AppID 不匹配"}
    if flat.get("trade_status") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return 200, {"order_id": flat.get("out_trade_no"), "settle": False}
    return 200, {"order_id": flat.get("out_trade_no"), "settle": True}
