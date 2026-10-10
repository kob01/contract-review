# 前端 · 信贷合同合规审查界面

React 19 + Vite + TypeScript + Tailwind v4，核心是 **PDF.js 原件渲染 + 风险条款高亮 + 复核交互**。

## 开发

```bash
npm install
npm run dev          # http://localhost:5173，/api 代理到 127.0.0.1:8000
npm run build        # tsc -b && vite build
npm run typecheck
npm run lint         # oxlint
```

后端未启动时顶栏会显示"后端未连接"，各页面自行处理错误提示。

## 页面

| 路由 | 页面 | 作用 |
| --- | --- | --- |
| `/` | 合同列表 | 状态、风险评分、待复核标记，重解析/审查/删除 |
| `/upload` | 上传审查 | 上传即解析即审查，跳转报告 |
| `/contracts/:id/reviews/:id` | 审查报告 | 左：PDF 高亮/条款文本 + 指标表；右：结论卡片与引用 |
| `/queue` | 法务复核 | 队列水位、误报率、逐条确认/驳回/调级 |
| `/rules` | 合规红线 | 规则库浏览与启停、阈值展示、**规则试算** |
| `/knowledge` | 法规知识库 | 占位登记清单、权威原文导入、检索自检 |

## 高亮是如何对齐的

后端结论带 `page_no + char_start/char_end`，但 PDF.js 的坐标系来自渲染时的
`getTextContent()`，两套下标不能直接换算。`components/PdfViewer.tsx` 的做法：

1. 逐页取文本块，拼成该页的纯文本，记录每个文本块的字符区间与视口矩形；
2. 建立"去掉所有空白后的下标 → 原始下标"映射；
3. 用去空白后的 `excerpt` 匹配，再映射回文本块，逐块画出高亮框。

因此模型返回的引用即使与 PDF 抽取文本存在空格/换行差异也能定位；
完全匹配不到时不高亮（宁可缺位也不假装命中）。

## 约定

- 类型集中在 `src/types/index.ts`，字段命名与后端 camelCase 序列化保持一致；
- 所有请求走 `src/api`，错误统一经 `apiErrorMessage` 归一化；
- 顶栏常驻展示 LLM / 向量供应商与是否 mock——降级状态必须一直可见。
