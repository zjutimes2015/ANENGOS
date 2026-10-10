# ANENGOS 租户知识库（阶段 1-4）

## 客户资料存哪里

每个租户独立空间，天然隔离：

```
/opt/anengos/workspace/tenants/<tenant_id>/knowledge/
├── docs/            # 原始文档文本（doc_id.txt）
├── meta.json        # 文档清单：文件名/标签/大小/时间/来源
└── index.json       # 关键词倒排索引（英文按词、中文 2-gram）
```

- 物理位置：腾讯云 `/opt/anengos/workspace`（Docker 挂载卷，随容器重启保留）
- 大文件（>2MB）阶段 1 拒绝，后续接腾讯云 COS 对象存储
- 审计：上传/删除写入该租户的 audit.jsonl

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/tenants/{tid}/knowledge` | 文档列表 + 统计（管理员/该租户） |
| POST | `/admin/api/tenants/{tid}/knowledge/upload` | `{filename, content(base64), tags[]}`，单文档 ≤2MB |
| POST | `/admin/api/tenants/{tid}/knowledge/search` | `{query}` → 命中文档 + 上下文片段 |
| POST | `/admin/api/tenants/{tid}/knowledge/ask` | **AI 问答（RAG）**：检索 + DeepSeek 生成，返回答案 + 来源 |
| POST | `/admin/api/tenants/{tid}/knowledge/{doc_id}/delete` | 删除文档（重建索引） |
| GET | `/api/tenant/knowledge` | **租户自助**：自己的文档列表（租户 token，无需知道租户 id） |
| POST | `/api/tenant/knowledge/upload` | 租户上传自己的资料（扣 2 信用分） |
| POST | `/api/tenant/knowledge/search` | 租户检索自己的资料（扣 1 信用分） |
| POST | `/api/tenant/knowledge/ask` | **租户 AI 问答**（扣 5 信用分 + 计 1 次任务用量，额度不足 429） |
| POST | `/api/tenant/knowledge/{doc_id}/delete` | 租户删除自己的文档 |

鉴权：管理员可管理任意租户（`/admin/...`）；租户 token 只能操作自己的（`/api/tenant/...`，跨租户 403）。租户已停用则 token 失效（401）。

## 检索说明

- **关键词检索（BM25）**：块级倒排索引（token→chunk tf），idf=log(1+N/(1+df))，score=Σtf·idf/√len
- **语义检索（向量，可插拔）**：设置以下环境变量后，上传时预计算全部 chunk 向量（≤32/批），问答时 query 向量对全量 chunk 余弦排序：
  - `ANENGOS_EMBEDDING_MODEL`（如 `BAAI/bge-m3`、`text-embedding-3-small`；DeepSeek 官方无 embedding）
  - `ANENGOS_EMBEDDING_BASE_URL`（默认取 `ANENGOS_BASE_URL`；硅基流动 `https://api.siliconflow.cn/v1`）
  - `ANENGOS_EMBEDDING_API_KEY`（默认取 `ANENGOS_API_KEY`）
  - **降级链**：向量缺失/embedding 失败 → 自动 BM25；LLM 不可达 → 返回纯检索来源
- **RAG 问答**：Top6 块拼上下文 → DeepSeek 生成（严格依据资料，不足则明说）；资料外问题返回"无相关内容"，不编造
- 存储：`chunks/{doc_id}.json`（切块）+ `chunk_index.json`（块级倒排）+ `vectors/{doc_id}.json`（向量，可选）

## MCP 端点（客户的企业 agent 直接问自己的资料）

把租户知识库包成 **MCP (Model Context Protocol) Streamable HTTP server**：Claude Code / Cursor / Windsurf / Claude Desktop 等任何 MCP 客户端，用**租户访问令牌**即可调用自己的知识库，无需网页。

| 项 | 说明 |
|---|---|
| 端点 | `POST /mcp`，鉴权 `Authorization: Bearer <租户token>`（管理员 token 不可用，与客户站一致） |
| 协议 | JSON-RPC 2.0：`initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`；协议版本 2025-06-18 |
| 工具 | `knowledge_list`（资料列表）· `knowledge_search`（关键词检索+片段，1 分）· `knowledge_ask`（RAG 问答，答案+来源，5 分+任务计数）· `knowledge_upload`（上传文本 ≤2MB，2 分） |
| 配额/审计 | 统一信用额度池计费（与网页/租户 API 同一把尺，见下）；所有调用按租户写 audit.jsonl |
| 隔离 | token 只命中自己的租户；跨租户不可见 |

**Claude Code 接入示例**（`~/.config/claude/mcp.json`）：

```json
{
  "mcpServers": {
    "anengos": {
      "url": "http://81.71.13.143:8080/mcp",
      "headers": { "Authorization": "Bearer <租户token>" }
    }
  }
}
```

之后在 Claude Code 里直接问："帮我查一下公司知识库里私有化部署的报价" —— 自动走 `knowledge_ask`。

## 信用额度池（统一计费，借鉴 AgentKey）

一套**月度信用分池**覆盖全部能力，按操作计分；月度池每月 1 日 UTC 重置，用尽后可购买**按量包**（extra credits，买断制不过期）。扣减顺序：先月度池，后按量包。

| 操作 | 单价 | 说明 |
|---|---|---|
| AI 问答 `knowledge_ask` | 5 分/次 | 检索 + DeepSeek 生成（同时计 1 次任务用量，兼容旧报表） |
| 任务执行 `task_run` | 10 分/次 | 总装线 /run 与 run-async（双闸门：任务次数配额 + 信用池） |
| 资料上传 `knowledge_upload` | 2 分/次 | 网页 / API / MCP 同一把尺 |
| 关键词检索 `knowledge_search` | 1 分/次 | 网页 / API / MCP 同一把尺 |
| `knowledge_list` / 列表类 | 免费 | 只读不扣 |

| 套餐 | 月度信用分 | 说明 |
|---|---|---|
| 试用版（免费） | 100 分 | 自助开通默认 |
| 团队版 ¥299/月 | 29,900 分 | ≈1,400 次问答 或 2,990 次检索 |
| 企业版 ¥1,500/月 | 150,000 分 | 高用量客户 |

**按量包**（超额加购，买断不过期）：`credits_1000`（¥9 / 1000 分）、`credits_5000`（¥40 / 5000 分）。

| 接口 | 说明 |
|---|---|
| `GET /api/billing/plans` | 公开：套餐 + 按量包 + 单价表（`credit_packs` / `rates`） |
| `POST /api/billing/order` `{plan: "credits_1000", tenant_id}` | 按量包下单（kind=credits，不升订阅配额） |
| `POST /admin/api/orders/{oid}/mark-paid` | 管理员补单结算 → `usage.extra_credits += 1000` |
| `GET /admin/api/usage` | 管理台/租户用量面板：含 `credits_used/credits_quota/credits_remaining/extra_credits` |
| `GET /api/client/session` | 客户站登录态 + 余额（本月剩余 / 按量包） |
| `GET /admin/api/usage/export.csv` | 账单 CSV 追加 `credits_used,credits_quota,extra_credits` 列 |
| Webhook | 告警比例取 max(任务比例, 信用池比例)；payload 含信用字段 |

- 管理端与管理员 token 操作免费（平台运营不计费）；额度不足统一 429 并提示购买按量包
- 管理员可在 `/admin/api/tenants/{tid}/quota` 设 `credits_per_month`，或直接 `extra_credits` 补给
- 月度归档：`billing[月] = {tasks, credits_used}`（与账单周期同一机制）

## 管理台

`/admin` →「客户知识库」卡片：选租户（管理员）→ 上传（文件+标签）→ 列表/删除 → 检索结果预览 + AI 问答。

## 客户站（独立门户，供客户自助使用）

给客户的独立小站，客户用**租户访问令牌（Token）**登录后即可向自己的知识库提问，无需经过管理台。

| 入口 | 说明 |
|---|---|
| `GET /client` | 未登录 → 登录页（client_login.html）；已登录 → 问答站（client.html） |
| `GET /api/client/session` | 登录态检测（`{ok: true, tenant_id}`） |
| `POST /api/client/login` | `{token, captcha_id, captcha_answer}`：人机验证码 + 租户 token → HttpOnly 会话 Cookie（12h）；5 次失败锁 15 分钟；管理员 token 不可登录客户站 |
| `POST /api/client/logout` | 销毁会话 |

- 客户站问答 UI：AI 问答（答案 + 来源 + 检索模式）、资料上传（≤2MB）、文档列表/删除、关键词检索；会话失效自动跳回登录页
- 数据按租户隔离：客户只能看到/问到自己的资料；管理员请走 `/admin`
- 页面文件：`client_login.html` / `client.html`（BASE 目录）

## 路线

- 阶段 1（已上线）：文件 + 关键词检索，零新依赖
- 阶段 2（已上线 0.2.4）：切块 + 块级 BM25 + DeepSeek 生成问答
- 阶段 3（已上线 0.2.4）：真语义检索（上传预计算向量 + query 余弦，多供应商可插拔）；租户自助知识库 API
- 阶段 4（已开发，待部署 0.3.0）：客户站门户（token 登录 + 问答 UI + 余额显示）；租户知识库 **MCP 端点**（客户的企业 agent 直接问自己的资料）；**信用额度池统一计费**（套餐含月度分、按量包加购、余额透明）；**Failover 缓冲重放**（任务队列持久化 `tasks.json`：失败自动退避重试 ≤3 次期间 `retrying` 可见，重启后 queued 自动续跑、running 等标记 `interrupted` 交管理员 `POST /admin/api/tasks/{id}/retry` 重放，绝不重复外部副作用；`POST /admin/api/tasks/{id}/cancel` 取消排队/运行/重试任务，重放不重复计费）；**分发闭环**（`/llms.txt` AI 可读产品说明 + `/install` 一行安装：用租户 token 访问即返回 Claude/Cursor 现成 MCP 配置与 curl 验证命令，服务端只存 token 哈希、配置回显调用者本次提交的 token）；**场景收敛**（`docs/POSITIONING.md`：定位"企业私有知识库问答 SaaS"，主页重构为聚焦卖点，能力分核心/商业/可信/边界四层，含"不做"清单）；外部向量库/对象存储、Rerank 精排、多语言文档解析

## 测试

`tests/test_knowledge.py` + `tests/test_rag.py` + `tests/test_semantic.py` + `tests/test_client.py` + `tests/test_mcp.py` + `tests/test_credits.py` + `tests/test_failover.py` + `tests/test_distribution.py`：上传/列表/检索/租户隔离（403）/删除/超限 413/切块/问答降级/向量预计算/向量优先排序/向量失败降级 BM25/租户 API 全流程（401/403/配额 429/用量计数）/客户站登录（验证码、错误令牌、管理员令牌拒绝、会话问答链路）/MCP（握手、鉴权 401、4 工具全链路、缺参与未知工具 isError、跨租户隔离、parse error）/信用额度池（默认额度、按操作计分、月度池+按量包扣减、耗尽 429、按量包结算、月度归档、CSV 信用列、余额透明）/Failover（自动重试成功且 attempts 可见、超限最终失败不丢任务、持久化+重启恢复 queued 自动续跑/running 标记 interrupted、手动 retry/cancel 端点、GET 动作 405）/分发（llms.txt 匿名完整、/install 鉴权 401、租户现成配置含 claude/cursor+curl、管理员指引不泄露 token）。全量 **100 passed**；另用官方 mcp SDK 客户端端到端冒烟通过。
