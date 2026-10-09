# ANENGOS Token 售卖与付款功能

> 状态：已上线（mock 演示通道）｜ 2026-10-09 公网验证 PASS

## 一、业务闭环（已实现）

```
客户 → 主页 /（定价区）→ 填租户 token → POST /api/billing/order 下单
     → /pay 收银台（mock 扫码）→ 模拟支付 → POST /api/billing/notify 回调
     → 订单标记 paid → 租户配额自动升级 → 管理台「账单与支付」可查可补单
```

- 套餐：试用（免费，走 /signup）/ 团队版 ¥299/月 / 企业版 ¥1,500/月
- 金额单位分（整数）；订单持久化到 orders.json（挂载卷，重启不丢）
- 回调幂等：重复 notify 不重复升级、不重复计费
- 管理员「标记已付」补单：线下收款 / 通道故障兜底
- 支付成功后租户配额取"新旧较大值"，并记入 billing 月度付费记录（与账单导出联动）

## 二、关键端点

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/billing/plans` | 公开 | 套餐与定价 |
| `POST /api/billing/order` | 公开 | `{plan, tenant_id(token)}` → 订单 |
| `GET /api/billing/order?id=od_xxx` | 公开 | 查订单状态 |
| `POST /api/billing/notify` | 公开* | `{order_id}` → 结算（mock） |
| `GET /admin/api/orders` | 管理员 | 订单列表 |
| `POST /admin/api/orders/{id}/mark-paid` | 管理员 | 手动补单 |

> *真实通道接入后，notify 改为验签回调（支付宝/微信签名校验），不再公开可调用。

## 三、接真实支付通道（支付宝/微信）步骤

1. **资质**：企业支付宝商户号 / 微信支付商户号（个人主体无法直接申请，需营业执照）
2. **新建适配器**：`billing_providers/alipay.py`（或 wechat.py），实现：
   - `create_payment(order) -> pay_url/qrcode`（调支付 API 生成付款码/跳转链接）
   - `verify_notify(raw_body, headers) -> order_id`（验签后返回订单号）
3. **切换通道**：`app.py` 中 `BILLING_PROVIDER = "alipay"`，并设置商户密钥环境变量（不进代码库）
4. **收银台**：pay.html 改为展示真实二维码/跳转支付网关，支付完成后由回调驱动轮询状态
5. **对账**：每日定时拉取支付平台账单，与 orders.json 核对（未完成，建议下一步）

## 四、个人主体过渡方案（无营业执照时）

- **聚合支付**（易支付/彩虹易支付等）：个人可申请，手续费约 1%~3%，支付到个人/对公账户；接入同样是"适配器 + 回调验签"模式，本系统结构可直接对接
- **线下收款 + 补单**：客户转账 → 管理台「标记已付」→ 配额自动升级（现已可用，零成本起步）
- ⚠️ 聚合支付平台有跑路/合规风险，资金量上来后建议升级官方商户号

## 五、下一步建议

- [ ] 确定收款通道（官方商户号 / 聚合支付 / 线下补单）
- [ ] 支付成功后向客户 Webhook 通知（复用告警 Webhook 通道）
- [ ] 订单分页 + 对账单导出
- [ ] 年付优惠（10%）结算逻辑
