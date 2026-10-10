# 架构说明

## 1. 数据模型

```
contracts                 一份合同：全文 + segments(条款切片, 带 char_start/char_end/page_no)
  │                          + extracted_metrics(指标与特征命中, 带计算过程与出处)
  ├── contract_reviews    一次审查运行：模型/提示词版本/检索快照/双通道计数/路由结果
  │     ├── review_findings   一条结论：来源(rule|llm|both)、交叉校验状态、置信度三元组
  │     │                     (confidence/llm_confidence/rule_confidence)、原文定位、
  │     │                     citations(JSON 引用快照)、人工复核状态
  │     └── human_review_tasks 复核队列项：触发原因、专家结论、模型快照(供 SFT 导出)
  │
red_line_rules            合规红线（数据驱动的规则 DSL）
legal_provisions          法条主表（一条一文 + pgvector 向量列）——引用的唯一可信来源
enforcement_cases         监管罚单（类比论证素材 + 向量列）
knowledge_ingest_runs     采集/入库审计记录
```

设计要点：

- **引用以 JSON 快照存进结论**，而不是外键关联。法规原文会被更新或废止，
  结论必须固定"当时引用的是哪个版本"，否则审计无法复现。
- **`ContractReview.retrieved_provisions`** 保存本轮实际进入提示词的法条清单，
  报告页据此展示"参考依据"，事后也能回答"模型当时看到了什么"。
- **`HumanReviewTask.model_snapshot`** 保存模型原始输出，配合专家结论即为标注样本。

## 2. 规则引擎的判定语义

`red_line_rules` 每行是一条可执行规则，`app/services/rules/engine.py` 解释执行：

| 字段 | 语义 |
| --- | --- |
| `metric_key` | 指标键（`max_annual_cost_rate`、`irr_annual_rate`、`monthly_fee_rate`…）或特征键（`final_interpretation` 等） |
| `operator` | `gt/gte/lt/lte/eq/exists/missing` |
| `threshold_source` | `static` / `lpr_four_multiple` / `regulatory_cost_ceiling` / `historical_hard_ceiling` / `nominal_rate_multiple` |
| `hard_block` | 硬拦截：结论定性不可被模型推翻 |
| `llm_fallback` | 指标缺失时是否把该规则作为重点检查项交给模型 |
| `keyword_patterns` | 正则弱信号，命中置信度 0.75，默认需 LLM + 人工确认 |
| `applies_to` | 适用合同种类，空表示全部 |
| `basis_refs` | 引用依据线索（法规名 + 条号），入库前不视为已核验 |

三种判定结果，缺一不可：

1. **hit** — 定量成立，置信度 1.0（或 `hit_confidence`）；
2. **miss** — 定量不成立；
3. **inconclusive** — 指标抽不出来。**绝不折算成"通过"**，
   而是带着规则文本进入模型提示词，并在结论里倾向转人工。

阈值来源为 `nominal_rate_multiple` 时，比较基准由"名义年利率 × 倍数"动态求得，
用于识别 IRR 与名义利率背离（隐性收费 / 砍头息）的结构性问题。

## 3. 检索（RAG）为什么是混合式

法条判断里"哪一部法的第几条"是**离散标识**，向量相似度会把"第二十六条"与"第二十七条"
判成近义，直接走向量会产生张冠李戴的引用。因此三路召回：

1. **条文直连**：规则 `basis_refs` 指定的（法规名, 条号）必然命中，分值最高；
2. **关键词**：法规名与条文内容 ILIKE + 条号精确匹配；
3. **向量**：pgvector 余弦距离（`1 - distance ≥ RAG_MIN_SIMILARITY` 才入选）。

融合后按分值排序取 Top-K。检索器实现为 LlamaIndex `BaseRetriever` 子类，
返回 `NodeWithScore(TextNode)`，但**相似度计算留在原生 SQL**：引用需要直接回连
`legal_provisions` 主键，另建一张影子向量表会让溯源校验失去意义。

## 4. 引用的三级可信度

| 状态 | 含义 | 前端表现 | 路由后果 |
| --- | --- | --- | --- |
| 已核验 | 命中条文且已有权威原文导入 | 绿色标签 | 可自动通过（非高风险时） |
| 占位登记 | 命中条目但原文未导入 | 橙色"待核验" | **强制转人工** |
| 未收录 | 知识库查不到该条 | 红色"引用未核验" | **强制转人工**，几乎必然是模型编造 |

## 5. 失败语义

系统对失败的处理原则是**失败必须可见**：

- 扫描件无文字层 → `Contract.status=failed` + `parse_error`，不产出"无风险"报告；
- 模型返回非 JSON / 不符合 schema → 该批次计入 `failed_batch_count`，其他批次继续；
  全部失败则整次审查 `failed`；
- 向量检索异常 → 降级为关键词召回并记 warning，审查不中断；
- 原文定位失败 → 结论保留但 `located=False`，转人工并在界面标注"未能定位原文"；
- 数据库不可用 → 服务仍启动，`/health` 返回 `degraded` 且 `database=unavailable`，
  便于排查依赖而不是容器反复崩溃重启。

## 6. 并发与事务边界

- 审查由 FastAPI `BackgroundTasks` 在响应返回后执行，**自带 `session_scope()`**；
  把请求级 session 传进后台任务是这类接口最常见的隐性 bug 来源。
- 模型调用阶段用 `asyncio.Semaphore` 并发，但**该阶段不碰数据库**
  （`AsyncSession` 不是并发安全的）；引用核验与落库在拿到结果后顺序执行。

## 7. 尚未落地（有意留白）

- 鉴权与多租户（`contracts.owner_id` 已预留）；
- Alembic 迁移（当前开发期用 `create_all` + 启动时建扩展/索引）；
- 罚单向量检索用于"同类违规已被处罚"的类比结论（`ReviewFinding.similar_case_ids` 已留字段）；
- 批注/修订模式的原文改写导出（docx 差异文件）；
- 采集器的选择器需按目标站点当前版面校准（见 `app/services/ingestion/regulator.py`）。
