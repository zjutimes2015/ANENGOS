# ANENGOS Token 售卖与付款功能

> 状态：已上线（默认 mock 演示通道；支付宝/微信官方适配器已就绪，密钥就位即可切换）｜ 2026-10-09 公网验证 PASS

## 一、业务闭环（已实现）

```
客户 → 主页 /（定价区）→ 填租户 token → POST /api/billing/order 下单
     → /pay 收银台（mock 模拟 / 支付宝二维码 / 微信 code_url）→ 支付
     → 回调结算或收银台轮询查单 → 订单标记 paid → 租户配额自动升级
     → 管理台「账单与支付」可查可补单
```

- 套餐：试用（免费，走 /signup）/ 团队版 ¥299/月 / 企业版 ¥1,500/月
- 金额单位分（整数）；订单持久化到 orders.json（挂载卷，重启不丢）
- 回调幂等：重复回调不重复升级、不重复计费
- 管理员「标记已付」补单：线下收款 / 通道故障兜底
- 支付成功后租户配额取"新旧较大值"，并记入 billing 月度付费记录（与账单导出联动）

## 二、支付通道（可插拔）

| 通道 | 切换方式 | 支付方式 | 说明 |
|---|---|---|---|
| mock | `ANENGOS_BILLING_PROVIDER=mock` | 收银台"模拟支付"按钮 | 演示/联调用，默认 |
| alipay | `ANENGOS_BILLING_PROVIDER=alipay` | 当面付二维码扫码 | 企业支付宝；主动查单 + 回调验签 |
| wechat | `ANENGOS_BILLING_PROVIDER=wechat` | Native 码 / 跳转链接 | 微信支付商户号；主动查单 + 回调验签 |

适配器位于 `billing_providers/`（mock.py / alipay.py / wechat.py），统一接口
`create_payment / query_payment / handle_notify`。新增通道只需加一个文件。

### 通道环境变量（密钥只进 .env，绝不进代码库）

```ini
# 通用
ANENGOS_BILLING_PROVIDER=mock        # mock | alipay | wechat

# 支付宝（企业支付宝开放平台）
ALIPAY_APP_ID=2026xxxxxxxxxxxxxxxx
ALIPAY_PRIVATE_KEY=<应用私钥 PEM，可单行 base64>
ALIPAY_PUBLIC_KEY=<支付宝公钥 PEM>
# ALIPAY_GATEWAY=https://openapi.alipay.com/gateway.do  # 沙箱联调可覆盖

# 微信支付（商户平台）
WECHAT_APPID=wxXXXXXXXXXXXXXXXX
WECHAT_MCH_ID=1900000001
WECHAT_SERIAL_NO=<商户 API 证书序列号>
WECHAT_PRIVATE_KEY=<商户 API 私钥 PEM>
WECHAT_PLATFORM_PUBLIC_KEY=<微信支付平台证书公钥 PEM>
WECHAT_API_V3_KEY=<APIv3 密钥，32 字节>
WECHAT_NOTIFY_URL=<备案+HTTPS 后填 https://siyu-ai.com/api/billing/notify>
```

### 说明

- **未备案阶段也能收款**：两通道都支持"收银台轮询主动查单"（GET /api/billing/order/{id}/poll），不依赖 HTTPS 回调；备案完成后配置 WECHAT_NOTIFY_URL 启用官方异步回调（带验签+解密）
- 下单时若通道密钥缺失/无效，返回 502 并给出原因，不会破坏 mock 演示
- 金额换算：支付宝 total_amount 元字符串（两位小数），微信 total 分（整数），内部统一以分存储

## 三、关键端点

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/billing/plans` | 公开 | 套餐与定价 |
| `POST /api/billing/order` | 公开 | `{plan, tenant_id(token), provider?}` → 订单 |
| `GET /api/billing/order?id=od_xxx` | 公开 | 查订单状态与支付参数 |
| `GET /api/billing/order/{id}/poll` | 公开 | 主动查单（真实通道轮询） |
| `POST /api/billing/notify` | 公开* | 支付回调（mock 直结 / 官方验签） |
| `GET /admin/api/orders` | 管理员 | 订单列表 |
| `POST /admin/api/orders/{id}/mark-paid` | 管理员 | 手动补单 |

> *真实通道启用后，notify 仅接受带合法签名的官方回调，不可任意调用。

## 四、商户资质申请清单

| 事项 | 支付宝 | 微信支付 |
|---|---|---|
| 主体 | 企业营业执照 + 对公账户 | 企业营业执照 + 对公账户 |
| 申请入口 | open.alipay.com（开放平台 → 网页/移动应用 → 签约当面付） | pay.weixin.qq.com（商户平台注册 → 申请 Native 支付） |
| 需要拿到的 | AppID、应用私钥（自生成）、支付宝公钥 | AppID、商户号、API 证书（序列号+私钥）、APIv3 密钥、平台证书 |
| 审核周期 | 1-3 个工作日 | 1-5 个工作日 |

## 五、个人主体过渡方案（无营业执照时）

- **聚合支付**（易支付/彩虹易支付等）：个人可申请，手续费约 1%~3%，支付到个人/对公账户；接入同样是"适配器 + 回调验签"模式，本系统结构可直接对接
- **线下收款 + 补单**：客户转账 → 管理台「标记已付」→ 配额自动升级（现已可用，零成本起步）
- ⚠️ 聚合支付平台有跑路/合规风险，资金量上来后建议升级官方商户号

## 六、下一步建议

- [ ] 用户提供商户密钥 → 填入服务器 .env → 切换 `ANENGOS_BILLING_PROVIDER` → 沙箱/小额真实支付验收
- [ ] 支付成功后向客户 Webhook 通知（复用告警 Webhook 通道）
- [ ] 订单分页 + 对账单导出
- [ ] 年付优惠（10%）结算逻辑
