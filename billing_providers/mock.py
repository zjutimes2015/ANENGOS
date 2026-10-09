"""mock 演示通道：pay.html 模拟支付，notify 直接结算（无签名）。"""
import json


def create_payment(order: dict) -> dict:
    """mock 通道不产生真实支付参数，仅标记订单待支付。"""
    return {"mock": True, "pay_url": "/pay?order_id=" + order["order_id"]}


def query_payment(order: dict) -> str:
    """mock 通道不查询，由收银台按钮驱动 notify。"""
    return "pending"


def handle_notify(raw_body: bytes, headers: dict) -> tuple[int, dict]:
    """mock 回调：raw_body 为 JSON {order_id}。返回 (code, 待结算信息)。"""
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return 400, {"error": "无效回调体"}
    oid = data.get("order_id")
    if not oid:
        return 400, {"error": "缺少 order_id"}
    return 200, {"order_id": oid, "settle": True}
