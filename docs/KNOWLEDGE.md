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
| GET | `/admin/api/tenants/{tid}/knowledge` | 文档列表 + 统计 |
| POST | `/admin/api/tenants/{tid}/knowledge/upload` | `{filename, content(base64), tags[]}`，单文档 ≤2MB |
| POST | `/admin/api/tenants/{tid}/knowledge/search` | `{query}` → 命中文档 + 上下文片段 |
| POST | `/admin/api/tenants/{tid}/knowledge/ask` | **AI 问答（RAG）**：检索 + DeepSeek 生成，返回答案 + 来源 |
| POST | `/admin/api/tenants/{tid}/knowledge/{doc_id}/delete` | 删除文档（重建索引） |

鉴权：管理员可管理任意租户；租户 token/会话只能操作自己的（403）。

## 检索说明

- **阶段 1（关键词）**：英文/数字按词、中文按 2-gram 建倒排索引，命中数打分，返回 Top10 + 上下文片段
- **阶段 2（RAG 问答）**：
  - 上传时文档切块（400 字/块、60 字重叠，按句/段优先），建块级倒排
  - ask = 块级 BM25 粗筛 Top30 → 取 Top6 → 拼上下文 → DeepSeek 生成（严格依据资料，不足则明说）
  - 可选向量精排：设置 `ANENGOS_EMBEDDING_MODEL`（如 `deepseek-embed`）后做语义重排（embedding 失败自动降级 BM25）
  - 资料外问题返回"无相关内容"，不编造
- 存储：`chunks/{doc_id}.json`（切块）+ `chunk_index.json`（块级倒排）

## 管理台

`/admin` →「客户知识库」卡片：选租户（管理员）→ 上传（文件+标签）→ 列表/删除 → 检索结果预览。

## 路线

- 阶段 1（已上线）：文件 + JSON + 关键词检索，零新依赖
- 阶段 2（已上线 0.2.4）：切块 + 块级 BM25 + DeepSeek 生成问答（"问我的资料"），向量精排可开关
- 阶段 3：外部向量库 / 对象存储，支持大文件与弹性；Rerank 精排

## 测试

`tests/test_knowledge.py` + `tests/test_rag.py`：上传/列表/中文与数字检索/租户隔离（403）/删除/超限 413/切块/块级检索/问答降级。全量 72 passed。
