# ANENGOS 租户知识库（阶段 1）

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
| POST | `/api/tenant/knowledge/upload` | 租户上传自己的资料（配额检查） |
| POST | `/api/tenant/knowledge/search` | 租户检索自己的资料 |
| POST | `/api/tenant/knowledge/ask` | **租户 AI 问答**（计 1 次任务用量，配额不足 429） |
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

## 管理台

`/admin` →「客户知识库」卡片：选租户（管理员）→ 上传（文件+标签）→ 列表/删除 → 检索结果预览。

## 路线

- 阶段 1（已上线）：文件 + 关键词检索，零新依赖
- 阶段 2（已上线 0.2.4）：切块 + 块级 BM25 + DeepSeek 生成问答
- 阶段 3（已上线 0.2.4）：真语义检索（上传预计算向量 + query 余弦，多供应商可插拔）；租户自助知识库 API（客户自己传/问自己的资料，配额计量）
- 阶段 4：外部向量库 / 对象存储，支持大文件与弹性；Rerank 精排；租户知识库 Web 页面

## 测试

`tests/test_knowledge.py` + `tests/test_rag.py` + `tests/test_semantic.py`：上传/列表/检索/租户隔离（403）/删除/超限 413/切块/问答降级/向量预计算/向量优先排序/向量失败降级 BM25/租户 API 全流程（401/403/配额 429/用量计数）。全量 77 passed。
