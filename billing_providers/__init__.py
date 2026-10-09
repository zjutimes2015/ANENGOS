"""支付通道包：mock / alipay / wechat 适配器。

统一接口：
    create_payment(order: dict) -> dict   # 返回 {qrcode|code_url|pay_url} 等支付参数
    query_payment(order: dict) -> str     # 返回支付状态："paid" | "pending" | "failed"
    handle_notify(raw_body, headers) -> (int, dict)  # 回调验签并返回结算结果

金额口径：订单 amount_cents 为分（整数）。支付宝需转元字符串，微信直接用分。
密钥一律从环境变量读取，禁止写入代码库。
"""
