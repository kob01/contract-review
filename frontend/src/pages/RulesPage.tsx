import { useCallback, useEffect, useMemo, useState } from "react";
import { apiErrorMessage } from "@/api/client";
import { dryRunRules, getThresholds, listRules, toggleRule } from "@/api";
import { Card, Empty, ErrorNote, RiskBadge } from "@/components/badges";
import { formatPercent } from "@/lib/labels";
import type {
  RedLineRule,
  RuleDryRunResponse,
  ThresholdSnapshot,
} from "@/types";

export default function RulesPage() {
  const [rules, setRules] = useState<RedLineRule[]>([]);
  const [thresholds, setThresholds] = useState<ThresholdSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [kind, setKind] = useState("consumer_loan");
  const [result, setResult] = useState<RuleDryRunResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [filter, setFilter] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [nextRules, nextThresholds] = await Promise.all([
        listRules(),
        getThresholds(),
      ]);
      setRules(nextRules);
      setThresholds(nextThresholds);
      setError(null);
    } catch (err) {
      setError(apiErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const visible = useMemo(
    () =>
      rules.filter(
        (rule) =>
          !filter ||
          rule.name.includes(filter) ||
          rule.code.toLowerCase().includes(filter.toLowerCase()),
      ),
    [rules, filter],
  );

  const run = async () => {
    setRunning(true);
    try {
      setResult(await dryRunRules(text, kind));
      setError(null);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setRunning(false);
    }
  };

  const toggle = async (rule: RedLineRule) => {
    try {
      await toggleRule(rule.code, !rule.isActive);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err));
    }
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(420px,560px)]">
      <div className="space-y-4">
        <Card
          title={`合规红线库（${visible.length}/${rules.length}）`}
          actions={
            <input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="搜索编码或名称"
              className="w-40 rounded-lg border border-slate-300 px-2 py-1 text-xs"
            />
          }
        >
          <ErrorNote message={error} />
          {thresholds && (
            <p className="mb-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
              当前口径：司法上限{" "}
              {formatPercent(thresholds.judicialProtectionCeiling)}（LPR{" "}
              {formatPercent(thresholds.lprOneYearRate)} ×{" "}
              {thresholds.lprMultiple}）· 监管成本红线{" "}
              {formatPercent(thresholds.regulatoryCostCeiling)} · 路由阈值{" "}
              {formatPercent(thresholds.confidenceRoutingThreshold, 0)}
            </p>
          )}
          <div className="max-h-[70vh] space-y-2 overflow-auto pr-1">
            {visible.map((rule) => (
              <div
                key={rule.code}
                className={`rounded-lg border p-3 ${rule.isActive ? "border-slate-200" : "border-slate-200 bg-slate-50 opacity-70"}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded bg-slate-900 px-2 py-0.5 font-mono text-xs text-white">
                    {rule.code}
                  </span>
                  <RiskBadge level={rule.severity} />
                  {rule.hardBlock && (
                    <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-700">
                      硬拦截
                    </span>
                  )}
                  {rule.appliesTo.length === 0 ? (
                    <span className="text-xs text-slate-400">适用全部合同</span>
                  ) : (
                    <span className="text-xs text-slate-400">
                      适用 {rule.appliesTo.join(" / ")}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => void toggle(rule)}
                    className="ml-auto rounded-lg border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100"
                  >
                    {rule.isActive ? "停用" : "启用"}
                  </button>
                </div>

                <h3 className="mt-2 text-sm font-medium text-slate-900">
                  {rule.name}
                </h3>
                <p className="mt-1 text-xs leading-relaxed text-slate-600">
                  {rule.guidanceText}
                </p>

                <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
                  {rule.metricKey && (
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono">
                      {rule.metricKey} {rule.operator}{" "}
                      {rule.thresholdValue ?? rule.thresholdSource}
                    </span>
                  )}
                  {rule.keywordPatterns.map((pattern) => (
                    <span
                      key={pattern}
                      className="max-w-[260px] truncate rounded bg-slate-100 px-1.5 py-0.5 font-mono"
                      title={pattern}
                    >
                      /{pattern}/
                    </span>
                  ))}
                  {rule.basisRefs.map((ref, index) => (
                    <span
                      key={index}
                      className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700"
                    >
                      《{(ref as { law_name?: string }).law_name ?? "未标注"}》
                      {(ref as { article_no?: string }).article_no ?? ""}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <div className="space-y-4">
        <Card title="规则试算（不调模型、不写审查记录）">
          <p className="mb-2 text-xs text-slate-500">
            粘贴一段合同文本，只看规则引擎的判定结果。改阈值后用它做回归，
            比重新跑一轮审查便宜得多。
          </p>
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={10}
            placeholder="示例：借款金额 100000 元，日利率万分之五，每月另按本金 1% 收取服务费，实际放款 95000 元……"
            className="w-full rounded-lg border border-slate-300 p-3 font-mono text-xs outline-none focus:border-slate-900"
          />
          <div className="mt-2 flex items-center gap-2">
            <select
              value={kind}
              onChange={(event) => setKind(event.target.value)}
              className="rounded-lg border border-slate-300 px-2 py-1.5 text-xs"
            >
              <option value="consumer_loan">消费信贷</option>
              <option value="small_loan">小额贷款</option>
              <option value="private_lending">民间借贷</option>
              <option value="guarantee">担保/抵押</option>
              <option value="partnership">助贷/合作</option>
            </select>
            <button
              type="button"
              disabled={running || text.trim().length < 10}
              onClick={() => void run()}
              className="ml-auto rounded-lg bg-slate-900 px-4 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-40"
            >
              {running ? "试算中…" : "运行规则引擎"}
            </button>
          </div>
        </Card>

        {result && (
          <Card
            title={`试算结果：命中 ${result.hits.length} 条 / 检查 ${result.checkedRuleCount} 条`}
          >
            {result.hits.length === 0 && (
              <Empty>没有红线命中。注意"无法定量判定"的条目。</Empty>
            )}
            <ul className="space-y-2">
              {result.hits.map((hit) => (
                <li
                  key={hit.ruleCode}
                  className="rounded-lg border border-slate-200 p-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded bg-slate-900 px-2 py-0.5 font-mono text-xs text-white">
                      {hit.ruleCode}
                    </span>
                    <RiskBadge level={hit.severity} />
                    {hit.hardBlock && (
                      <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-700">
                        硬拦截
                      </span>
                    )}
                    <span className="text-xs text-slate-500">
                      置信度 {Math.round(hit.confidence * 100)}%
                    </span>
                  </div>
                  <h4 className="mt-1.5 text-sm font-medium text-slate-900">
                    {hit.title}
                  </h4>
                  <p className="mt-1 text-xs text-slate-600">{hit.analysis}</p>
                  <p className="mt-1 text-xs text-emerald-700">
                    {hit.suggestion}
                  </p>
                  {hit.metricValue !== null &&
                    hit.metricValue !== undefined && (
                      <p className="mt-1 text-[11px] text-slate-500">
                        实测 {formatPercent(hit.metricValue)} ·{" "}
                        {hit.thresholdDesc}
                      </p>
                    )}
                  {hit.excerpt && (
                    <p
                      className="mt-1 truncate text-[11px] text-slate-400"
                      title={hit.excerpt}
                    >
                      原文：{hit.excerpt}
                    </p>
                  )}
                </li>
              ))}
            </ul>

            {result.inconclusive.length > 0 && (
              <div className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
                <div className="mb-1 font-semibold">
                  无法定量判定（将作为重点检查项交给模型与人工）
                </div>
                <ul className="list-disc pl-5">
                  {result.inconclusive.map((item) => (
                    <li key={item.code}>
                      {item.code} {item.name}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}
