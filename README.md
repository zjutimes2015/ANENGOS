# ANENGOS — 企业私有知识库问答 SaaS（原名 AGENTOS / AI 生产力操作系统）

**定位（借鉴清单第 6 步收敛）**：让企业把资料交给 ANENGOS，然后用 Claude / Cursor / API / 网页
四种方式向 AI 提问自己的知识库。核心能力 = 私有知识库 + RAG 问答 + MCP 接入 + 信用额度池计费；
总装线/审批/审计/Failover 是支撑"可信"的底座，不是卖点。详见 `docs/POSITIONING.md`（含"不做"清单）。

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

## MCP：让客户的企业 agent 直接问自己的知识库

租户知识库已包成 **MCP (Streamable HTTP) 端点** `POST /mcp`：Claude Code / Cursor / Windsurf / Claude Desktop 等任何 MCP 客户端，用**租户访问令牌**即可调用，无需网页。

```json
// ~/.config/claude/mcp.json
{
  "mcpServers": {
    "anengos": {
      "url": "https://你的域名/mcp",
      "headers": { "Authorization": "Bearer <租户token>" }
    }
  }
}
```

工具：`knowledge_list` / `knowledge_search` / `knowledge_ask`（RAG 问答，计配额）/ `knowledge_upload`。管理员 token 不可用（走 `/admin`）。详见 `docs/KNOWLEDGE.md`。

## 多智能体总装线：Adapter + 统一 Schema（借鉴 AgentKey 的跨 provider 模式）

Codex / 豆包 / Grok 等外部智能体通过统一 `AgentAdapter` 接口接入：入参统一 `(task, workspace)`，出参统一 **AgentResult Schema** `{ok, provider, status, text, artifact_paths, error, meta}`——管理台、互审、审计不再解析各家字符串；`health()` 提供可观测性（`/admin/api/agents` 与 `/health` 展示各智能体就绪状态），`describe()` 供注册器自动生成工具 Schema。新增一个智能体 = 写一个 `connectors/xxx.py` 实现三个方法，主程序零改动。注册器自动挂载 `{name}.submit`（副作用，需审批）与 `{name}.health`（只读）。测试 `tests/test_agent_schema.py`。

## 信用额度池计费（借鉴 AgentKey 的统一计量）

一套**月度信用分池**覆盖全部能力：AI 问答 5 分/次、任务执行 10 分/次、上传 2 分/次、检索 1 分/次；月度池每月 1 日 UTC 重置，超额可买**按量包**（¥9/1000 分、¥40/5000 分，买断不过期）。套餐含额度：试用 100 分 / 团队版 ¥299/月 29,900 分 / 企业版 ¥1,500/月 150,000 分。余额在客户站、用量面板、CSV 账单与 Webhook 全链路透明可见。详见 `docs/KNOWLEDGE.md`「信用额度池」。

## Failover 缓冲重放：任务队列断点续跑 + 失败自动重放（借鉴 AgentKey）

异步任务队列不再丢任务：**客户端只看到完整结果或完整失败**。

- **失败自动重放**：执行抛异常（provider 抖动、构建失败）自动指数退避重试（1s/2s/4s…封顶 30s，最多 3 次），期间状态 `retrying` + `next_retry_at` 全程可见；超限给出最终错误，任务不消失。
- **断点续跑（持久化）**：任务落盘 `tasks.json`；进程重启后 `queued`（从未执行，无副作用）自动续跑，`running/retrying/waiting_approval`（可能已产生外部副作用）标记 `interrupted`，由管理员 `POST /admin/api/tasks/{id}/retry` 手动重放，绝不重复外部调用。
- **手工控制**：`POST /admin/api/tasks/{id}/retry`（error/interrupted/cancelled 可重放）、`POST /admin/api/tasks/{id}/cancel`（queued/running/retrying 可取消）；任务列表含 `attempts`/`next_retry_at`。
- **不重复计费**：费用在提交时已扣，重放/续跑不再扣费。

测试 `tests/test_failover.py`（4 用例：自动重试成功、超限最终失败、持久化+重启恢复、retry/cancel 端点）。

## 分发闭环：llms.txt + 一行安装（借鉴 AgentKey 的 AI 分发）

让客户 5 分钟接入：**发现（llms.txt）→ 开通（管理台自助）→ 一行安装（/install）→ 验证（MCP）→ 计费（信用池）**。

- **`/llms.txt`**（匿名可读，llmstxt.org 规范）：AI 可读的产品说明——入口、MCP 端点、工具清单、接入步骤，让 Claude/Cursor 等 agent 能"发现"你的能力。
- **`/install`**（需登录，回显你的租户 token 生成现成配置）：返回 Claude Code / Cursor 的 `mcpServers` 配置 + 一条 `curl` 快速验证命令 + 工具与计费说明。安全策略：服务端只存 token 哈希，"用什么 token 登录，配置里就用什么"，不额外落明文。
- 公网地址由 `ANENGOS_PUBLIC_URL` 控制（`.env.example` 已加），部署到域名后自动指向 `https://siyu-ai.com/mcp`。

测试 `tests/test_distribution.py`（4 用例：llms.txt 匿名完整、/install 鉴权、租户现成配置、管理员指引）。

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
