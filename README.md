# 信贷合同合规审查 Agent

面向消费信贷 / 小贷 / 助贷合作协议的合规审查系统。核心立场是：
**LLM 擅长语言理解，不擅长严密的法律逻辑推理**，因此所有硬性指标（利率、费用、期限）
由确定性规则引擎判定，模糊条款（"不合理加重消费者责任"）才交给 LLM + RAG，
并且**所有模型结论都必须经引用核验与置信度路由，未达标者强制人工法务复核**。

---

## 快速开始

Windows（在 PowerShell 里执行，不是 bash/Git Bash）：

```powershell
Copy-Item .env.example .env     # 默认无需任何密钥即可跑通全链路（离线 Mock）
.\scripts\dev.ps1               # 起 pgvector + 后端 + 前端
```

macOS / Linux / Git Bash：

```bash
cp .env.example .env            # 默认无需任何密钥即可跑通全链路（离线 Mock）
./scripts/dev.sh                # 起 pgvector + 后端 + 前端
```

> 两个脚本按脚本类型选执行器，不能互相代跑：在 bash 里跑 `./scripts/dev.ps1` 会被 sh 解析并报
> `syntax error near unexpected token 'newline'`。仅起数据库用 `-DbOnly` / `--db-only`。

拉起数据库与完整后端：

```bash
docker compose up -d db         # PostgreSQL 16 + pgvector
cd backend && uv run uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev
```

访问 <http://localhost:5173>，上传 `samples/consumer-loan-demo.txt` 即可看到完整审查报告。

> Docker Hub 不可直连时，在 `.env` 里设 `PGVECTOR_IMAGE=docker.m.daocloud.io/pgvector/pgvector:pg16`；
> 数据库默认映射宿主机 **5433**（容器内 Postgres 固定监听 5432，只能改宿主机侧端口）。
> 改端口时下列四处必须一致，否则后端会连到旧端口：`docker-compose.yml` 的 `${DB_PORT:-5433}` 默认值、
> `.env` 的 `DB_PORT`、`.env` 的 `DATABASE_URL`、`backend/app/core/config.py` 的 `database_url` 默认值。

## 技术栈

| 层    | 选型                                                                          |
| ----- | ----------------------------------------------------------------------------- |
| 前端  | React 19 + Vite + TypeScript + Tailwind v4 + **PDF.js**（原件渲染与风险高亮） |
| 后端  | FastAPI + SQLAlchemy 2.0（async）+ **uv**                                     |
| Agent | 规则引擎（自研 DSL 解释器）+ **LlamaIndex** 检索器契约 + 四路召回 RRF 融合 |
| 检索  | **pgvector**（Ollama bge-m3，1024 维）+ **Elasticsearch BM25**（jieba 应用侧分词）+ **TEI cross-encoder** 重排 |
| 模型  | OpenAI 兼容接口（当前 GLM-5.3-Flash；DeepSeek / 通义 / Kimi / vLLM / Ollama 可切）；复核模型独立配置槽位 |
| 数据  | PostgreSQL 16 + pgvector（法条、罚单、红线、结论、复核任务同源一库）          |

## 双通道审查流程

```
上传合同
  → 文本抽取（保留页码与字符偏移）
  → 条款切分（segment + char range）
  → 确定性指标抽取（日/月/年化利率、综合成本、IRR、本金预扣比例、霸王条款特征）
  → 规则引擎判定硬红线（>24%、>4×LPR、砍头息、最终解释权、放弃抗辩…）
  → RAG 检索法条（条文直连 + pgvector 向量 + ES/jieba BM25 + ILIKE 兜底，四路 RRF 融合）
  → cross-encoder 重排（bge-reranker-v2-m3，低相关候选剔除）
  → LLM 定性判断（只允许引用检索清单内的条文，数值不允许自己算）
  → 交叉校验（冲突以规则引擎为准，冲突计数入指标）
  → 引用核验（回库比对条文；占位/查不到 = 引用未核验）
  → 结论复核（独立 judge 模型，只加严不减严）
  → 置信度路由（<0.8 或引用未核验或高风险或复核质疑 → 人工复核队列）
  → 报告 + PDF 高亮 + 批注建议
```

## 关键设计

**1. 规则引擎优先，而不是让模型读数字。**
"月利率 3 分 + 按初始本金每月收 1% 服务费 + 分 12 期"这类安排，模型很容易只报出表面的 36%。
`app/services/rules/metrics.py` 用现金流 IRR、复利年化、本金预扣比例等口径算出**最不利年化值**，
`app/services/rules/engine.py` 按数据库中 `red_line_rules` 的阈值判定，规则可被法务在页面试算与调参。

**2. 阈值是配置，不是代码。**
4×LPR、24%、36%、置信度阈值全部来自 `Settings`（可环境变量覆盖）。
LPR 每月变动时不改规则数据，只改一个参数；报告页展示当前口径，避免"同一合同不同结论"无法解释。

**3. 引用必须回库核验。**
提示词只给出带 `provisionId` 的检索清单，模型回填该 ID；`app/services/citation.py` 把引用回库比对：
命中且有权威原文 → 已核验；命中但只有占位登记 → 未核验；查不到 → 未核验并强制转人工。
**本仓库不内置任何条文原文**，详见 [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)。

**4. 溯源高亮是产品能力，不是演示效果。**
`segments`/指标/结论都带 `page_no + char_start/char_end`，前端 PDF.js 按页重建文本块坐标后匹配高亮。
定位失败的结论会明确标注"原文未能定位"，而不是假装高亮成功。

**5. 复核队列构成数据飞轮。**
每条转人工的结论都记录触发原因与模型快照，`GET /api/v1/human-review/export/sft`
可导出专家修正样本（确认/误报/调级 + 意见），用于后续微调与偏好对齐。
`GET /api/v1/human-review/stats` 给出误报率与调级率，是阈值调整的依据。

## 离线模式与可信度边界

未配置密钥与本地服务时：LLM 走启发式 Mock、向量走确定性哈希 Mock、ES/TEI 不可达则少一路召回。
**链路是通的，但检索相似度不具备语义含义**，健康检查、界面顶栏与报告都会显式标注这一点
（`RAG_USE_MOCK_EMBEDDING=true` 时 `/health` 直接返回 `degraded`）。
真实跑审查需要：`LLM_API_KEY`、本地 Ollama（bge-m3）或远端 embedding、Elasticsearch、TEI 重排服务，
并把 `RAG_USE_MOCK_EMBEDDING=false` 与**导入权威条文原文**（否则检索只命中占位登记，全部结论进人工队列）。

> 思考型模型（如 GLM-5.3-Flash）的推理 token 计入 `max_tokens`：实测单批审查思考要 1.2~1.9 万 token，
> 预算给小会表现为"只思考不输出"的空回；`LLM_MAX_TOKENS` 默认 32768，
> 可用 `LLM_REASONING_EFFORT=low` 换几十秒延时与数倍成本差距（代价是结论会漏）。

## 目录结构

```
backend/
  app/api/routes/       contracts / reviews / human_review / redlines / knowledge / health
  app/models/           Contract, ContractReview, ReviewFinding(+citations),
                        LegalProvision, EnforcementCase, RedLineRule, HumanReviewTask
  app/services/
    rules/              metrics.py 指标抽取 · engine.py 规则解释器 · builtin_redlines.py 内置红线
    rag/                embeddings.py · retriever.py(混合检索) · ingest.py(条文入库)
    llm/                base/factory/openai_compatible/mock/prompts
    citation.py         引用核验
    findings.py         交叉校验 + 置信度路由（纯函数，可单测）
    review_pipeline.py  双通道编排
  tests/                规则/指标/切分/路由/LLM 层 57 条单测
frontend/
  src/components/PdfViewer.tsx   PDF.js 渲染 + 文本块坐标高亮
  src/pages/                     列表 / 上传 / 报告 / 复核队列 / 红线库(试算) / 知识库
docs/                  ARCHITECTURE.md · DATA_SOURCES.md
samples/               consumer-loan-demo.txt 演示样本
```

## API 概览

| 方法 | 路径                                      | 说明                                        |
| ---- | ----------------------------------------- | ------------------------------------------- |
| POST | `/api/v1/contracts`                       | 上传并解析（抽取文本 + 指标）               |
| POST | `/api/v1/contracts/{id}/reviews`          | 发起双通道审查（后台执行，返回 202）        |
| GET  | `/api/v1/reviews/{id}/report`             | 结论 + 溯源引用 + 参考条文 + 指标告警       |
| GET  | `/api/v1/human-review/queue`              | 待复核队列                                  |
| POST | `/api/v1/human-review/tasks/{id}/resolve` | 提交复核结论（回写并计入误报率）            |
| GET  | `/api/v1/human-review/export/sft`         | 导出标注样本                                |
| POST | `/api/v1/redlines/dry-run`                | 规则试算（不调模型、不写记录）              |
| POST | `/api/v1/knowledge/provisions`            | 导入权威条文原文                            |
| GET  | `/api/v1/knowledge/search`                | 混合检索自检（展示召回路径与是否 mock）     |
| GET  | `/api/v1/health`                          | 连通性 + 降级程度（LLM/向量/规则库/法条库） |

## 测试

```bash
cd backend
uv run pytest              # 57 passed（指标、切分定位、规则判定、交叉校验、路由、LLM 层）
uv run ruff check .
```

## 合规使用前提

- 本系统输出**辅助性合规提示**，不构成法律意见；高风险结论需法务确认后对外使用。
- 内置红线中的"24% 窗口指导""4×LPR 司法保护上限"等口径存在适用边界
  （持牌机构与民间借贷不同），请按主体资质与产品类型选择规则集。
- 监管公开信息采集默认关闭，`INGESTION_ENABLED=true` 前需自行确认目标站点 robots 与使用条款。
