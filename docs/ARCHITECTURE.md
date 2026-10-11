# 架构说明

## 1. 数据模型

```
contracts                 一份合同：全文 + segments(条款切片, 带 char_start/char_end/page_no)
  │                          + extracted_metrics(指标与特征命中, 带计算过程与出处)
  ├── contract_reviews    一次审查运行：模型/提示词版本/检索快照/双通道计数/路由结果
  │     │                 + judge_model/judge_call_count/judge_failed_count（结论复核）
  │     │                 + retrieval_channels（本次真正参与召回的通道，降级可审计）
  │     ├── review_findings   一条结论：来源(rule|llm|both)、交叉校验状态、置信度三元组
  │     │                     (confidence/llm_confidence/rule_confidence)、原文定位、
  │     │                     citations(JSON 引用快照)、judge_verdict(复核裁定)、人工复核状态
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

| 字段               | 语义                                                                                                             |
| ------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `metric_key`       | 指标键（`max_annual_cost_rate`、`irr_annual_rate`、`monthly_fee_rate`…）或特征键（`final_interpretation` 等）    |
| `operator`         | `gt/gte/lt/lte/eq/exists/missing`                                                                                |
| `threshold_source` | `static` / `lpr_four_multiple` / `regulatory_cost_ceiling` / `historical_hard_ceiling` / `nominal_rate_multiple` |
| `hard_block`       | 硬拦截：结论定性不可被模型推翻                                                                                   |
| `llm_fallback`     | 指标缺失时是否把该规则作为重点检查项交给模型                                                                     |
| `keyword_patterns` | 正则弱信号，命中置信度 0.75，默认需 LLM + 人工确认                                                               |
| `applies_to`       | 适用合同种类，空表示全部                                                                                         |
| `basis_refs`       | 引用依据线索（法规名 + 条号），入库前不视为已核验                                                                |

三种判定结果，缺一不可：

1. **hit** — 定量成立，置信度 1.0（或 `hit_confidence`）；
2. **miss** — 定量不成立；
3. **inconclusive** — 指标抽不出来。**绝不折算成"通过"**，
   而是带着规则文本进入模型提示词，并在结论里倾向转人工。

阈值来源为 `nominal_rate_multiple` 时，比较基准由"名义年利率 × 倍数"动态求得，
用于识别 IRR 与名义利率背离（隐性收费 / 砍头息）的结构性问题。

## 3. 检索（RAG）为什么是混合式

法条判断里"哪一部法的第几条"是**离散标识**，向量相似度会把"第二十六条"与"第二十七条"
判成近义，直接走向量会产生张冠李戴的引用。因此四路召回 + 一次重排：

1. **条文直连**：规则 `basis_refs` 指定的（法规名, 条号）必然命中，融合时拿封顶分，
   且**不参与重排剔除**——它是规则自带的引用基础，不是相似度发现；
2. **向量**：pgvector 余弦距离（`1 - distance ≥ RAG_MIN_SIMILARITY` 才入选），
   默认后端是本地 Ollama 的 bge-m3（1024 维 + HNSW/cosine）；
3. **BM25**：Elasticsearch。分词在应用侧用 jieba 完成，把切好的词写进 `whitespace`
   分析器字段：目标集群通常装不上 analysis 插件，而写入与查询共用同一套词典更可控；
4. **关键词兜底**：法规名与条文内容 ILIKE + 条号精确匹配，ES 不可达时顶上。

融合用 **RRF（Reciprocal Rank Fusion）**而不是分数加权：cosine 与 BM25 分值不同量纲、
随数据分布漂移，名次才可比；多通道同时命中的条目自然累加，等价于交叉确认。

融合后的候选再交给 **cross-encoder 重排**（TEI 的 bge-reranker-v2-m3）：召回解决"别漏"，
重排解决"谁真的相关"。低于 `RAG_RERANK_MIN_SCORE` 的候选直接剔除，这一步拦下的正是
"靠一个共享词命中"的噪声——本机实测相关条文 ~0.99，占位记录与不相关条文 ≤0.08。

检索器实现为 LlamaIndex `BaseRetriever` 子类，返回 `NodeWithScore(TextNode)`，
但**相似度计算留在原生 SQL**：引用需要直接回连 `legal_provisions` 主键，
另建一张影子向量表会让溯源校验失去意义。ES 只当"名次来源"，命中的 id 一律回主表取原文。

每次审查都会落 `retrieval_channels`（本次真正参与的通道），因此降级是可事后审计的事实，
而不是只写在日志里的一句话。

## 4. 引用的三级可信度

| 状态     | 含义                       | 前端表现         | 路由后果                           |
| -------- | -------------------------- | ---------------- | ---------------------------------- |
| 已核验   | 命中条文且已有权威原文导入 | 绿色标签         | 可自动通过（非高风险时）           |
| 占位登记 | 命中条目但原文未导入       | 橙色"待核验"     | **强制转人工**                     |
| 未收录   | 知识库查不到该条           | 红色"引用未核验" | **强制转人工**，几乎必然是模型编造 |

## 5. 结论复核（LLM judge）

双通道之外再挂一道独立复核：另一个模型只回答"这条结论站不站得住"，
输出 `agree / citationOk / suggestedRiskLevel / confidence / reason`。

- **只加严不减严**：复核判不成立或 `confidence < JUDGE_MIN_CONFIDENCE` 时把结论拉进
  人工队列（原因 `judge_disputed`），但复核给出的等级建议**不改写 `risk_level`**——
  等级口径以规则引擎为准，否则复核模型自己的一次错就能把已定级的高风险改低。
- **与主模型分槽**（`JUDGE_*`）：只有来路不同才能互为独立信号；两家共用一个模型时，
  报告必须能说出这个事实（`ContractReview.judge_model`）。
- **缺席不等于通过**：没配复核模型、或某批调用失败时，对应结论写
  `judgeVerdict.judged=false` 并计入 `judge_failed_count`，不会被当成"复核已过"。

## 6. 失败语义

系统对失败的处理原则是**失败必须可见**：

- 扫描件无文字层 → `Contract.status=failed` + `parse_error`，不产出"无风险"报告；
- 模型返回非 JSON / 不符合 schema → 该批次计入 `failed_batch_count`，其他批次继续；
  全部失败则整次审查 `failed`；
- 思考型模型只输出思考不输出正文（推理 token 撞满 `max_tokens`）→ 报错信息带上
  `finish_reason` 与思考 token 数，直接指向"调大 max_tokens 还是拆小提示词"；
- 向量检索异常 → 降级为其余通道并记 warning，审查不中断；
- ES / TEI 不可达 → 少一路召回或少一步重排，同样只记 warning；非连接类异常会打
  带栈的 error，不会被当成"服务又挂了"长期掩盖；
- 原文定位失败 → 结论保留但 `located=False`，转人工并在界面标注"未能定位原文"；
- 数据库不可用 → 服务仍启动，`/health` 返回 `degraded` 且 `database=unavailable`，
  便于排查依赖而不是容器反复崩溃重启。

## 7. 并发与事务边界

- 审查由 FastAPI `BackgroundTasks` 在响应返回后执行，**自带 `session_scope()`**；
  把请求级 session 传进后台任务是这类接口最常见的隐性 bug 来源。
- 模型调用阶段用 `asyncio.Semaphore` 并发，但**该阶段不碰数据库**
  （`AsyncSession` 不是并发安全的）；引用核验与落库在拿到结果后顺序执行。

## 8. 尚未落地（有意留白）

- 鉴权与多租户（`contracts.owner_id` 已预留）；
- Alembic 迁移（开发期用 `create_all` + `session.py` 里的启动补丁：
  `_ADDED_COLUMNS` 补新列、`align_vector_dimensions()` 对齐向量列维度）；
- 罚单向量检索用于"同类违规已被处罚"的类比结论（`ReviewFinding.similar_case_ids` 已留字段，
  BM25 索引目前只覆盖条文）；
- 前端尚未展示 `judgeVerdict` 与 `retrievalChannels`；
- 批注/修订模式的原文改写导出（docx 差异文件）；
- 采集器的选择器需按目标站点当前版面校准（见 `app/services/ingestion/regulator.py`）。
