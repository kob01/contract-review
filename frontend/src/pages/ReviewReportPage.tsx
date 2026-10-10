import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiErrorMessage } from "@/api/client";
import { contractFileUrl, getContract, getReport, getThresholds } from "@/api";
import PdfViewer, { type PdfHighlight } from "@/components/PdfViewer";
import FindingCard from "@/components/FindingCard";
import { Card, Empty, ErrorNote } from "@/components/badges";
import { formatPercent, riskHighlight } from "@/lib/labels";
import type {
  ContractDetail,
  Finding,
  ReviewReport,
  ThresholdSnapshot,
} from "@/types";

const RUNNING = new Set(["pending", "running"]);

export default function ReviewReportPage() {
  const { contractId = "", reviewId = "" } = useParams();
  const [contract, setContract] = useState<ContractDetail | null>(null);
  const [report, setReport] = useState<ReviewReport | null>(null);
  const [thresholds, setThresholds] = useState<ThresholdSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeFindingId, setActiveFindingId] = useState<string | null>(null);
  const [view, setView] = useState<"pdf" | "text">("pdf");
  const [sourceFilter, setSourceFilter] = useState<
    "all" | "rule" | "llm" | "both"
  >("all");

  const load = useCallback(async () => {
    try {
      const [nextReport, nextContract] = await Promise.all([
        getReport(reviewId),
        getContract(contractId),
      ]);
      setReport(nextReport);
      setContract(nextContract);
      setError(null);
      return nextReport;
    } catch (err) {
      setError(apiErrorMessage(err));
      return null;
    }
  }, [reviewId, contractId]);

  useEffect(() => {
    void load();
    getThresholds()
      .then(setThresholds)
      .catch(() => setThresholds(null));
  }, [load]);

  // 审查在后台执行，状态未终结前轮询
  useEffect(() => {
    if (!report || !RUNNING.has(report.review.status)) return;
    const timer = window.setInterval(async () => {
      const next = await load();
      if (next && !RUNNING.has(next.review.status)) window.clearInterval(timer);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [report, load]);

  useEffect(() => {
    if (contract && contract.fileSuffix !== ".pdf") setView("text");
  }, [contract]);

  const findings = report?.findings ?? [];

  const filtered = useMemo(
    () =>
      sourceFilter === "all"
        ? findings
        : findings.filter((item) => item.source === sourceFilter),
    [findings, sourceFilter],
  );

  const highlights: PdfHighlight[] = useMemo(
    () =>
      filtered.map((finding: Finding) => ({
        id: finding.id,
        excerpt: finding.excerpt,
        color: riskHighlight[finding.riskLevel],
        active: finding.id === activeFindingId,
        pageNo: finding.pageNo,
      })),
    [filtered, activeFindingId],
  );

  const activeFinding =
    findings.find((item) => item.id === activeFindingId) ?? null;

  if (error && !report) {
    return (
      <div className="space-y-4">
        <ErrorNote message={error} />
        <Link to="/" className="text-sm text-blue-600 hover:underline">
          返回合同列表
        </Link>
      </div>
    );
  }

  if (!report || !contract)
    return <p className="text-sm text-slate-500">加载中…</p>;

  const { review } = report;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <Link to="/" className="text-xs text-slate-500 hover:text-slate-900">
          ← 合同列表
        </Link>
        <h1 className="text-lg font-semibold text-slate-900">
          {contract.title}
        </h1>
        <span className="text-xs text-slate-500">
          {contract.fileName} · {contract.pageCount} 页 ·{" "}
          {contract.charCount.toLocaleString()} 字符
        </span>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span className="rounded-lg bg-slate-100 px-2 py-1 text-slate-600">
            模型 {review.llmModel || "—"} · 提示词 {review.promptVersion}
          </span>
          <span className="rounded-lg bg-slate-100 px-2 py-1 text-slate-600">
            耗时 {review.elapsedSeconds.toFixed(1)}s
          </span>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-lg border border-slate-300 px-3 py-1 hover:bg-slate-50"
          >
            刷新
          </button>
        </div>
      </header>

      {RUNNING.has(review.status) && (
        <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
          审查执行中（{review.status === "pending" ? "排队" : "双通道分析"}
          ），页面每 3 秒自动刷新。
        </div>
      )}

      {review.status === "failed" && (
        <ErrorNote message={`审查失败：${review.errorMessage ?? "未知原因"}`} />
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(420px,560px)]">
        {/* 左栏：原件 + 高亮 */}
        <div className="space-y-4">
          <Card
            title="合同原件与风险定位"
            actions={
              <div className="flex gap-1 text-xs">
                {(["pdf", "text"] as const).map((mode) => (
                  <button
                    key={mode}
                    type="button"
                    onClick={() => setView(mode)}
                    className={`rounded-lg px-3 py-1 ${
                      view === mode
                        ? "bg-slate-900 text-white"
                        : "border border-slate-300 text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {mode === "pdf" ? "PDF 视图" : "条款文本"}
                  </button>
                ))}
              </div>
            }
          >
            {view === "pdf" && contract.fileSuffix === ".pdf" ? (
              <div className="max-h-[70vh] overflow-auto">
                <PdfViewer
                  fileUrl={contractFileUrl(contract.id)}
                  highlights={highlights}
                />
              </div>
            ) : (
              <div className="max-h-[70vh] space-y-3 overflow-auto">
                {contract.segments.map((segment) => {
                  const related = findings.filter(
                    (item) => item.segmentIndex === segment.index,
                  );
                  return (
                    <div
                      key={segment.index}
                      className={`rounded-lg border p-3 ${
                        related.length
                          ? "border-red-200 bg-red-50/40"
                          : "border-slate-200"
                      }`}
                    >
                      <div className="flex items-center justify-between text-xs text-slate-500">
                        <span className="font-medium text-slate-700">
                          {segment.index}. {segment.title}
                        </span>
                        <span>第 {segment.pageNo} 页</span>
                      </div>
                      <p className="prose-contract mt-2 text-sm text-slate-700">
                        {segment.text}
                      </p>
                      {related.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {related.map((item) => (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => setActiveFindingId(item.id)}
                              className="rounded-full px-2 py-0.5 text-xs text-white"
                              style={{
                                backgroundColor: riskHighlight[item.riskLevel],
                              }}
                            >
                              {item.title}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title="确定性指标抽取结果（规则引擎的输入）">
            <div className="mb-3 grid gap-2 text-xs sm:grid-cols-3">
              {thresholds && (
                <>
                  <MetricCell
                    label="民间借贷司法上限"
                    value={formatPercent(thresholds.judicialProtectionCeiling)}
                    hint={`LPR ${formatPercent(thresholds.lprOneYearRate)} × ${thresholds.lprMultiple}`}
                  />
                  <MetricCell
                    label="监管综合成本红线"
                    value={formatPercent(thresholds.regulatoryCostCeiling)}
                    hint="持牌机构口径"
                  />
                  <MetricCell
                    label="置信度路由阈值"
                    value={formatPercent(
                      thresholds.confidenceRoutingThreshold,
                      0,
                    )}
                    hint="低于该值转人工"
                  />
                </>
              )}
            </div>

            {contract.metrics.length === 0 ? (
              <Empty>未抽取到任何数值指标或风险表述。</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-slate-500">
                    <tr className="border-b border-slate-200">
                      <th className="py-1.5 pr-3">指标</th>
                      <th className="py-1.5 pr-3">数值</th>
                      <th className="py-1.5 pr-3">计算过程</th>
                      <th className="py-1.5">原文片段</th>
                    </tr>
                  </thead>
                  <tbody>
                    {contract.metrics.map((item, index) => (
                      <tr
                        key={`${item.key}-${index}`}
                        className="border-b border-slate-100"
                      >
                        <td className="py-1.5 pr-3 font-medium text-slate-700">
                          {item.label}
                        </td>
                        <td className="py-1.5 pr-3 tabular-nums text-slate-900">
                          {item.display || "命中"}
                        </td>
                        <td className="py-1.5 pr-3 text-slate-500">
                          {item.derivation || "—"}
                        </td>
                        <td
                          className="max-w-[240px] truncate py-1.5 text-slate-500"
                          title={item.excerpt}
                        >
                          {item.excerpt}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {report.metricWarnings.length > 0 && (
              <ul className="mt-3 space-y-1 text-xs text-amber-700">
                {report.metricWarnings.map((warning) => (
                  <li key={warning}>· {warning}</li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        {/* 右栏：结论 */}
        <div className="space-y-4">
          <Card title="审查结论概览">
            <div className="flex items-end gap-4">
              <div>
                <div className="text-4xl font-semibold tabular-nums text-slate-900">
                  {review.riskScore}
                  <span className="ml-1 text-sm font-normal text-slate-400">
                    /100
                  </span>
                </div>
                <div className="text-xs text-slate-500">
                  风险评分（越高越可控）
                </div>
              </div>
              <div className="ml-auto grid grid-cols-3 gap-2 text-center text-xs">
                <Count
                  label="高"
                  value={review.highRiskCount}
                  tone="text-red-600"
                />
                <Count
                  label="中"
                  value={review.mediumRiskCount}
                  tone="text-amber-600"
                />
                <Count
                  label="低"
                  value={review.lowRiskCount}
                  tone="text-blue-600"
                />
              </div>
            </div>

            <p className="mt-3 text-sm leading-relaxed text-slate-700">
              {review.summary || "—"}
            </p>

            <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
              <Cell label="规则命中" value={review.ruleHitCount} />
              <Cell label="模型结论" value={review.llmFindingCount} />
              <Cell
                label="通道冲突"
                value={review.conflictCount}
                alert={review.conflictCount > 0}
              />
              <Cell label="检索条文" value={review.retrievedChunkCount} />
              <Cell label="模型调用" value={review.llmCallCount} />
              <Cell
                label="失败批次"
                value={review.failedBatchCount}
                alert={review.failedBatchCount > 0}
              />
              <Cell
                label="最低置信度"
                value={`${Math.round(review.minConfidence * 100)}%`}
              />
              <Cell
                label="待复核"
                value={review.pendingHumanCount}
                alert={review.pendingHumanCount > 0}
              />
            </div>

            {review.needsHumanReview && (
              <Link
                to="/queue"
                className="mt-3 block rounded-lg bg-orange-50 px-3 py-2 text-xs text-orange-800 ring-1 ring-inset ring-orange-600/20 hover:bg-orange-100"
              >
                有 {review.pendingHumanCount} 条结论需要法务确认，进入复核队列 →
              </Link>
            )}
          </Card>

          <Card
            title={`结论清单（${filtered.length}）`}
            actions={
              <select
                value={sourceFilter}
                onChange={(event) =>
                  setSourceFilter(event.target.value as typeof sourceFilter)
                }
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs"
              >
                <option value="all">全部来源</option>
                <option value="rule">仅规则引擎</option>
                <option value="llm">仅模型</option>
                <option value="both">双通道一致</option>
              </select>
            }
          >
            {filtered.length === 0 ? (
              <Empty>
                {review.status === "succeeded"
                  ? "没有符合条件的结论。"
                  : "等待审查完成后自动展示结论。"}
              </Empty>
            ) : (
              <div className="max-h-[70vh] space-y-3 overflow-auto pr-1">
                {filtered.map((finding) => (
                  <FindingCard
                    key={finding.id}
                    finding={finding}
                    active={finding.id === activeFindingId}
                    onSelect={(id) =>
                      setActiveFindingId(id === activeFindingId ? null : id)
                    }
                  />
                ))}
              </div>
            )}
          </Card>

          <Card title="本轮检索到的参考条文">
            {activeFinding ? (
              <p className="mb-2 text-xs text-slate-500">
                已选中结论：{activeFinding.title}
              </p>
            ) : null}
            {report.referenceProvisions.length === 0 ? (
              <Empty>
                本轮未检索到可用条文。请检查法规知识库是否已导入原文。
              </Empty>
            ) : (
              <ul className="max-h-64 space-y-2 overflow-auto pr-1 text-xs">
                {report.referenceProvisions.map((item, index) => (
                  <li
                    key={item.provisionId ?? index}
                    className="rounded-lg border border-slate-200 p-2"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-slate-800">
                        《{item.lawName}》{item.articleNo ?? ""}
                      </span>
                      <span
                        className={`rounded-full px-2 py-0.5 text-[11px] ${
                          item.verified
                            ? "bg-emerald-100 text-emerald-700"
                            : "bg-red-100 text-red-700"
                        }`}
                      >
                        {item.verified ? "已核验" : "未核验"}
                      </span>
                      {item.matchKinds?.length ? (
                        <span className="text-[11px] text-slate-400">
                          召回：{item.matchKinds.join(" / ")}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-1 line-clamp-3 text-slate-600">
                      {item.quote}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

const Cell = ({
  label,
  value,
  alert,
}: {
  label: string;
  value: string | number;
  alert?: boolean;
}) => (
  <div
    className={`rounded-lg px-2 py-1.5 ${alert ? "bg-red-50 text-red-700" : "bg-slate-50 text-slate-700"}`}
  >
    <div className="text-[11px] opacity-70">{label}</div>
    <div className="tabular-nums font-medium">{value}</div>
  </div>
);

const Count = ({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: string;
}) => (
  <div className="rounded-lg bg-slate-50 px-3 py-2">
    <div className={`text-xl font-semibold tabular-nums ${tone}`}>{value}</div>
    <div className="text-[11px] text-slate-500">{label}风险</div>
  </div>
);

const MetricCell = ({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) => (
  <div className="rounded-lg border border-slate-200 px-3 py-2">
    <div className="text-[11px] text-slate-500">{label}</div>
    <div className="font-semibold tabular-nums text-slate-900">{value}</div>
    <div className="text-[11px] text-slate-400">{hint}</div>
  </div>
);
