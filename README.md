# ANENGOS — 可落地的 agent 操作系统（原名 AGENTOS）

把 learn-claude-code 的 12 课机制（内核）与 Cloudflare OS 的能力模型（治理）融合，
并长出「浏览器之手」（Chromium + Playwright）的自托管、可商用 agent 工作台。

```
agentos/
├── kernel/            # 内核：agent 循环、工具分发、规划（learn-claude-code 机制）
├── governance/        # 治理：能力授权、Gatekeeper、异步审批、审计（Cloudflare OS 模型）
├── connectors/        # 连接层：浏览器（Chromium/Playwright）、外部服务 Gatekeeper
├── gadgets/           # 应用层：私有沙箱 Gadget 基类
└── tests/             # 单元测试（内核 3 + 浏览器 3）
```

## 浏览器「手」：长出手，但手也受刹车管辖

底层是 Chromium（github.com/chromium/chromium——Chrome/Edge/Opera 及国产浏览器的开源内核），
通过 Playwright 驱动（可插拔，测试用 MockDriver）：

- **域名介绍制**：agent 只能访问你「介绍」的域名（默认零权限，`browser.open` 的 scope 即白名单）
- **只读放行**：`browser.open` / `browser.extract` 放行并写审计
- **副作用审批**：`browser.click` 等动作模拟放行、排队等待用户批准（simulate-first）
- **审计回放**：每个浏览动作留痕，可回放

启用真实 Chromium：

```bash
pip install -e ".[playwright]"
playwright install chromium
# 把 demo_browser.py / 测试里的 MockBrowserDriver 换成 PlaywrightBrowserDriver 即可
```

## 快速开始

```bash
pip install -e ".[dev]"
python demo.py               # 最小闭环：能力检查 → 审批 → 审计
python demo_browser.py       # 浏览器闭环：域名白名单 → 拒绝/放行 → 点击审批
pytest -q                    # 6 个测试全绿
```

## 最小闭环演示（demo.py）

1. agent 请求调用 `github.read`（已被介绍）→ 放行并审计；
2. agent 请求调用 `github.write`（有副作用）→ 异步审批：模拟执行、排队待批；
3. agent 请求调用 `slack.post`（未被介绍）→ 拒绝并说明原因；
4. 用户批量批准排队动作，审计日志可回放。

## 设计要点

- **默认零权限**：`governance/capability.py` 中每个 actor 的能力注册表默认为空，必须显式 `introduce`。
- **模拟优先审批**：`governance/approval.py` 对副作用动作先返回模拟结果，agent 不阻塞，用户稍后批量批准/拒绝。
- **append-only 审计**：`governance/audit.py` 把每个动作追加写入 JSONL，可回放。
- **内核与治理接线**：`kernel/loop.py` 的每次工具调用都先过 `gatekeeper.check`。
- **浏览器之手**：`connectors/browser.py` 域名级白名单 + 审计 + 点击异步审批。

## 商业属性（开源核心 + 企业版双许可）

本仓库为 **Apache-2.0 开源核心**，可直接学习、改造与二次开发。面向企业售卖的形态建议：

| 版本 | 形态 | 定价建议 |
|---|---|---|
| 开源核心 | 本仓库（Apache-2.0） | 免费 |
| 标准版 | 私有化部署包：Docker + 文档 + 支持 | 按年授权 |
| 专业版 | 治理全开 + 多租户 + 审计台 + 浏览器安全策略 | 按席位 + LLM 透传 |
| 定制版 | 垂直行业定制（金融/政务合规） | 合同价 |

差异化卖点：能力模型（默认零权限 + 介绍制）、异步审批（simulate-first）、append-only 审计、
浏览器之手全程受治理管辖——正是企业「敢放开 agent」所需的安全基线。

> 许可提醒：Chromium 为 BSD 风格开源许可，Playwright 为 Apache-2.0；
> 本项目仅调用两者接口，不修改其源码，商用前请各自核对许可条款。
