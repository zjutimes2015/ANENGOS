# ANENGOS — 可落地的 agent 操作系统（原名 AGENTOS）

把 learn-claude-code 的 12 课机制（内核）与 Cloudflare OS 的能力模型（治理）融合，
并长出「浏览器之手」（Chromium + Playwright）的自托管、可商用 agent 工作台。

```
agentos/
├── kernel/            # 内核：agent 循环、工具分发、规划、真实模型接入（learn-claude-code 机制）
├── governance/        # 治理：能力授权、Gatekeeper、异步审批、审计（Cloudflare OS 模型）
├── connectors/        # 连接层：浏览器（Chromium/Playwright）、外部服务 Gatekeeper
├── gadgets/           # 应用层：私有沙箱 Gadget 基类
├── app.py             # 极简 HTTP 服务：/health + /run（Docker 部署入口）
└── tests/             # 单元测试（内核/浏览器/LLM/端到端 共 11 个）
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
pytest -q                    # 11 个测试全绿（内核/浏览器/LLM/端到端）
```

## 真实模型接入（OpenAI 兼容）

内核已内置 OpenAI 兼容客户端（`kernel/llm.py`，只依赖标准库），豆包 / DeepSeek / Qwen / OpenAI 通用：

```bash
# 方式一：命令行演示
set ANENGOS_API_KEY=sk-xxx
set ANENGOS_BASE_URL=https://ark.cn-beijing.volces.com/api/v3   # 豆包示例
set ANENGOS_MODEL=ep-xxxxxxxx
python demo_real.py "在工作区写 hello.txt，内容 hello anengos，然后列出来"

# 方式二：HTTP 服务（产品化形态）
set ANENGOS_API_TOKEN=<一串随机长字符串>   # 必填：防裸奔
python app.py            # 默认 8080，/health 存活检查，POST /run 提交任务
curl -X POST http://127.0.0.1:8080/run -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" -d "{\"query\":\"把今天的工作整理成清单\"}"
```

**鉴权规则**：未配置 `ANENGOS_API_TOKEN` 时 `/run` 拒绝对外服务（返回 503）；配置后必须带 `Authorization: Bearer <token>` 或 `X-API-Token: <token>`，否则 401；`/health` 不鉴权（供健康检查）。未配置 API key 时自动回退到脚本化演示，仓库开箱可跑；集成测试用本地 mock 模型验证全链路（模型调用 → 工具执行 → 治理审批），不依赖外网。

## Docker 一键部署

```bash
cp .env.example .env     # 填入 ANENGOS_API_KEY 等
docker compose up -d --build
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/run -H "Content-Type: application/json" -d "{\"query\":\"把今天的工作整理成清单\"}"
```

- 端口：`ANENGOS_PORT`（默认 8080）；工作区挂载 `./workspace`，审计挂载 `./audit`（企业要的"数据不出域 + 留痕"）
- 镜像自带 healthcheck；模型厂商可换，代码零改动


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
