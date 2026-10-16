# pgvector → Qdrant 向量库迁移

## 摘要与关键决策
- 架构：**Qdrant 作加速层**，只存 `provision_id + 4096维向量 + payload(is_active/doc_type/is_sample)`；检索命中后一律 [_load_by_ids()](file:///d:/ai/contract-review/backend/app/services/rag/retriever.py#L504-L515) 回 Postgres `legal_provisions` 取原文，**引用溯源不变**（复刻 [_rows_by_bm25](file:///d:/ai/contract-review/backend/app/services/rag/retriever.py#L477-L489) 的模式）。
- 移除 pgvector：删除 `embedding` 列、`CREATE EXTENSION vector`、HNSW 索引 DDL、python 依赖 `pgvector`。
- 维度：`EMBEDDING_DIMENSIONS=4096`、`EMBEDDING_TRUNCATE=false`（Ollama qwen3-embedding:8b 本就返回原生 4096，向量层代码无需改）。
- 降级：Qdrant 不可用时稠密通道丢弃，exact_ref/keyword/bm25 照常（与 ES 同款 `Unavailable` 异常口径）。
- 数据量：非占位权威条文当前仅 1 条、enforcement_cases 0 条，全量重嵌成本极低。
- 预期管理（重要）：4096 相对 1024(MRL) 的精度增益很小；"长文本更精准"的主要杠杆仍是切分粒度与 cross-encoder 精排，本次不动这两处。

## 分期（建议按此顺序交付，每期可独立验证）
- 期1：依赖+配置+compose+新 vectorstore 客户端+collection 初始化与全量重嵌。
- 期2：检索链路改走 Qdrant（retriever/deps）。
- 期3：入库链路改双写 Postgres+Qdrant、删列与会话层清理（ingest/knowledge/mappers/models/session）。
- 期4：health、docs、前端标签、测试适配。

## 依赖与部署
- [pyproject.toml](file:///d:/ai/contract-review/backend/pyproject.toml)：新增 `"qdrant-client>=1.12"`（用 `AsyncQdrantClient`，REST）；移除 `"pgvector>=0.3.6"`；改 L23 注释（不再"由 pgvector SQL 完成"）。随后 `uv sync`。
- [docker-compose.yml](file:///d:/ai/contract-review/docker-compose.yml)：新增 `qdrant` 服务
  - `image: ${QDRANT_IMAGE:-docker.m.daocloud.io/qdrant/qdrant:v1.12.4}`（沿用 daocloud 镜像站约定）
  - `container_name: contract-review-qdrant`，`ports: "16333:6333"`（REST）、`"6334:6334"`（gRPC，仅占位；客户端走 REST）
  - `volumes: qdrantdata:/qdrant/storage`；顶部 `volumes:` 加 `qdrantdata`
  - healthcheck 探 `GET /readyz`；镜像若无 curl/wget 则退化为 `depends_on: service_started`
  - backend/worker 增 `depends_on: qdrant` 与 `environment: QDRANT_URL: http://qdrant:6333`

## 配置层
- [config.py](file:///d:/ai/contract-review/backend/app/core/config.py)：
  - `embedding_dimensions: int = 4096`；`embedding_truncate: bool = False`；更新相关注释（删 2000 维上限/MRL 叙述）。
  - 新增：`qdrant_url: str = "http://localhost:16333"`、`qdrant_collection: str = "contract_provisions"`、`qdrant_cases_collection: str = "contract_enforcement_cases"`、`qdrant_api_key: str = ""`、`qdrant_timeout_seconds: float = 30.0`。
  - 删除 pgvector 专用项 `vector_index_type`（连同 session 中用法一并去）。
  - `rag_min_similarity` 注释更新：Qdrant cosine 分值同为 0~1，但 4096 原生分布需重测。
- [.env](file:///d:/ai/contract-review/.env) / [.env.example](file:///d:/ai/contract-review/.env.example)：`EMBEDDING_DIMENSIONS=4096`、`EMBEDDING_TRUNCATE=false`；新增 `QDRANT_URL/QDRANT_COLLECTION/QDRANT_CASES_COLLECTION/QDRANT_API_KEY/QDRANT_IMAGE`；移除 `VECTOR_INDEX_TYPE`；改向量段注释去掉"pgvector HNSW"。

## 新增向量库客户端 `backend/app/services/rag/vectorstore.py`
照 [sparse.py](file:///d:/ai/contract-review/backend/app/services/rag/sparse.py) 的共享单例+配置指纹+优雅降级范式实现：
- `class VectorStoreUnavailable(RuntimeError)`。
- `@dataclass DenseDoc`: `record_id, vector, is_active, is_sample, doc_type`。
- `class QdrantVectorStore`：持一个 `AsyncQdrantClient(url, api_key, timeout)` 与两个 collection 名（provisions/cases）。
  - `async ensure_collection(name, *, size)`：不存在则 `create_collection(vectors_config=VectorParams(size=size, distance=Distance.Cosine))`；**若已存在但 size 不等于 `settings.embedding_dimensions`（1024→4096 迁移）→ drop + 重建并记 warning**（解决换维旧向量空间混用）。
  - `async upsert(name, docs)`、`async delete(name, id)`、`async search(name, vector, *, limit, only_active) -> list[tuple[str,float]]`（`query_filter` 按 `is_active`，返回 `ScoredPoint.id/.score`）、`async count(name)`、`async aclose()`。
- `build_vectorstore(settings)`、模块级 `shared_vectorstore(settings)`（key=`url::collections::dimensions`）、`aclose_shared_vectorstore()`。

## 模型 / 建表 / 会话
- [models/knowledge.py](file:///d:/ai/contract-review/backend/app/models/knowledge.py)：删两表 `embedding` 列与 `Vector`/`settings` 导入；改模块 docstring L4-8（不再"pgvector 向量列"）。
- [db/session.py](file:///d:/ai/contract-review/backend/app/db/session.py)：移除 `ensure_vector_extension/align_vector_dimensions/ensure_vector_indexes` 及 `_HNSW/_IVFFLAT/_VECTOR_*` 常量；`init_models()` 简化为 `create_all + ensure_added_columns`；新增幂等 `ALTER TABLE legal_provisions/enforcement_cases DROP COLUMN IF EXISTS embedding`（清理既有开发库；无 Alembic，一次性）。

## 入库链路（双写）
- [ingest.py](file:///d:/ai/contract-review/backend/app/services/rag/ingest.py)：
  - `upsert_provision`/`upsert_enforcement_case`：去掉内联 `embedder` 与 `row.embedding` 写入（只写 Postgres）。
  - 删除 pgvector 版 `rebuild_missing_embeddings`/`rebuild_all_embeddings`；新增 `sync_vector_store(session, store, *, limit)`：全量重嵌 active 且 `is_sample=False` 的条文（占位不入向量），embed→`store.upsert(provisions)`，分批；cases 同理 `sync_cases_vector_store`。
- [api/routes/knowledge.py](file:///d:/ai/contract-review/backend/app/api/routes/knowledge.py)：
  - `import_provision`：commit 后除现有 ES `index_provisions` 外，增 `store.upsert` 单条向量（失败仅 warning，不回滚，顺带修正记忆中"内联 embedding 异常整条回滚"的缺陷）。
  - `delete_provision`：ES 删除旁加 `store.delete`。
  - `/reindex`：向量侧重建改调 `sync_vector_store`（+ ES `sync_sparse_index`），移除 `full`/`rebuild_*` 的 pgvector 语义。
  - `/search` 响应补 `dense_store="qdrant"`（或复用 `embedding_provider`）。
- [api/mappers.py](file:///d:/ai/contract-review/backend/app/api/mappers.py)：`to_provision_out`/`to_case_out` 的 `has_embedding=row.embedding is not None` → 改为 `not row.is_sample`（占位不入向量的等价代理，避免每行查 Qdrant）。

## 检索链路
- [retriever.py](file:///d:/ai/contract-review/backend/app/services/rag/retriever.py)：
  - `ProvisionRetriever.__init__` 增 `vector: QdrantVectorStore | None = None`。
  - `_rows_by_vector(vector)`：改 `store.search(provisions, vector, limit=width, only_active=...)` → `_load_by_ids` 回表 → 用 Qdrant score 过 `rag_min_similarity`；捕获 `VectorStoreUnavailable`/Exception 降级（不阻断其余通道）。
  - `_cases_by_vector`：同理走 cases collection + 新增 `_load_cases_by_ids`。
  - `_prefetch_network` 仍并发 embedding（Qdrant 查询依赖向量，紧随其后）。
- [deps.py](file:///d:/ai/contract-review/backend/app/api/deps.py)：`build_retriever` 注入 `vector=shared_vectorstore(app_settings)`；`close_search_clients()` 增 `aclose_shared_vectorstore()`。
- [main.py](file:///d:/ai/contract-review/backend/app/main.py) lifespan：`init_models()` 后建 store、`ensure_collection`（触发 1024→4096 重建），并以 `sync_vector_store` 替换现 `rebuild_missing_embeddings` 调用；Qdrant 不可达记 warning、降级启动。

## 健康检查 / 前端 / 文档
- [health.py](file:///d:/ai/contract-review/backend/app/api/routes/health.py)：增 Qdrant 通道状态（连通/降级）上报；`describe_*` 保持不建连接。
- 前端：[types/index.ts](file:///d:/ai/contract-review/frontend/src/types/index.ts) 注释 `pgvector` 措辞、[badges.tsx](file:///d:/ai/contract-review/frontend/src/components/badges.tsx) `dense_source` 示例改 `qdrant`（小、可选）。
- 文档：[ARCHITECTURE.md](file:///d:/ai/contract-review/docs/ARCHITECTURE.md)（L44/60/71-79/135/224）、[README.md](file:///d:/ai/contract-review/README.md)（L20/57/127）、[DATA_SOURCES.md](file:///d:/ai/contract-review/docs/DATA_SOURCES.md)（L67 阈值语境）——pgvector→Qdrant、维度叙述改原生 4096 无截断。

## 测试
- 现有 [tests/](file:///d:/ai/contract-review/backend/tests) 里若构造 `LegalProvision(embedding=...)` 或依赖 `_rows_by_vector` SQL 会断：改用内存版 fake `QdrantVectorStore`（照 fake embedder 的做法）；更新 [fixtures.py](file:///d:/ai/contract-review/backend/tests/fixtures.py)、[test_retrieval_fusion.py](file:///d:/ai/contract-review/backend/tests/test_retrieval_fusion.py) 等。
- 新增用例：collection 维度不符触发重建；Qdrant 不可用 → 稠密降级、其余通道仍在；双写后按 id 回表取到权威原文。

## 风险与回滚
- 一致性：Qdrant 与 Postgres 双写非事务；沿用 ES 策略——Postgres 先 commit，向量写失败仅告警、可用 `/reindex` 补，不把"缺向量"表现成结论。
- 无 Alembic：删列走幂等 DDL；生产需补正式迁移。
- 端口：宿主 16333/6334 与现有服务核对（当前无占用）；如 6334 gRPC 用不到可省。
- 回滚：改动集中且可逆；保留一个可切回 pgvector 的分支点（`_rows_by_vector` 旧实现）。

## 验收
1. `ollama pull qwen3-embedding:8b`（已具备）；`docker compose up -d qdrant`。
2. 启动 backend+worker：collection 以 4096 建立、权威条文全量入 Qdrant；Postgres 无 embedding 列；`/health` 报 Qdrant 通道就绪。
3. `GET /knowledge/search?q=...`：`channels_used` 含 `vector`，结果 `provisionId` 能回连 Postgres 原文。
4. 停掉 Qdrant：检索降级不报错、exact_ref/keyword/bm25 仍返回。
5. `ruff check app` 与 `pytest` 全绿。

## 待确认
- Qdrant 是否需要多语言/混合稀疏（本次仍由 ES 承担 BM25，Qdrant 只做稠密）；以及 `qdrant-client` 版本是否固定 v1.12.x。