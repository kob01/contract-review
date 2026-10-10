import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiErrorMessage } from "@/api/client";
import { listReviewQueue, resolveReviewTask, reviewStats } from "@/api";
import {
  Card,
  Empty,
  ErrorNote,
  RiskBadge,
  SourceBadge,
} from "@/components/badges";
import { formatTime, reasonLabels } from "@/lib/labels";
import type { HumanReviewTask, RiskLevel } from "@/types";

type Verdict = "confirmed" | "false_positive" | "need_adjustment";

export default function ReviewQueuePage() {
  const [tasks, setTasks] = useState<HumanReviewTask[]>([]);
  const [stats, setStats] = useState<Record<string, number | null>>({});
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<
    Record<string, { verdict: Verdict; comment: string; level: string }>
  >({});

  const refresh = useCallback(async () => {
    try {
      const [nextTasks, nextStats] = await Promise.all([
        listReviewQueue(),
        reviewStats(),
      ]);
      setTasks(nextTasks);
      setStats(nextStats);
      setError(null);
    } catch (err) {
      setError(apiErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const draftFor = (task: HumanReviewTask) =>
    drafts[task.id] ?? { verdict: "confirmed", comment: "", level: "" };

  const updateDraft = (
    task: HumanReviewTask,
    patch: Partial<{ verdict: Verdict; comment: string; level: string }>,
  ) =>
    setDrafts((current) => ({
      ...current,
      [task.id]: { ...draftFor(task), ...patch },
    }));

  const submit = async (task: HumanReviewTask) => {
    const draft = draftFor(task);
    setBusyId(task.id);
    try {
      await resolveReviewTask(task.id, {
        verdict: draft.verdict,
        comment: draft.comment,
        adjustedRiskLevel: draft.level || undefined,
      });
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-4">
      <Card title="复核队列水位">
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
          <Stat label="待复核" value={stats["pending"]} highlight />
          <Stat label="已复核" value={stats["resolved"]} />
          <Stat label="误报数" value={stats["false_positive"]} />
          <Stat label="调级数" value={stats["adjusted"]} />
          <Stat
            label="误报率"
            value={percentOf(stats["false_positive_rate"])}
          />
        </div>
        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          误报率与调级分布是阈值调整的依据：误报率高说明置信度阈值过松或提示词约束不足；
          调级集中说明模型对风险分级的判断与专家不一致。每次复核都会写入
          <code className="mx-1 rounded bg-slate-100 px-1">
            human_review_tasks
          </code>
          并可导出为标注样本。
        </p>
      </Card>

      <ErrorNote message={error} />

      {tasks.length === 0 ? (
        <Empty>队列干净：当前没有置信度不足或引用未核验的结论。</Empty>
      ) : (
        <div className="space-y-3">
          {tasks.map((task) => {
            const draft = draftFor(task);
            return (
              <Card key={task.id} className="!p-0">
                <div className="flex flex-wrap items-center gap-2">
                  {task.riskLevel && (
                    <RiskBadge level={task.riskLevel as RiskLevel} />
                  )}
                  {task.findingSource && (
                    <SourceBadge source={task.findingSource} />
                  )}
                  <span className="rounded-full bg-orange-100 px-2 py-0.5 text-xs text-orange-700">
                    {reasonLabels[task.reason]}
                  </span>
                  <span className="text-xs text-slate-500">
                    置信度 {Math.round((task.findingConfidence ?? 0) * 100)}% ·
                    创建于 {formatTime(task.createdAt)}
                  </span>
                  <Link
                    to={`/contracts/${task.contractId}/reviews/${task.reviewId}`}
                    className="ml-auto text-xs text-blue-600 hover:underline"
                  >
                    查看完整报告
                  </Link>
                </div>

                <h3 className="mt-2 text-sm font-semibold text-slate-900">
                  {task.findingTitle}
                </h3>
                {task.findingExcerpt && (
                  <blockquote className="mt-2 border-l-2 border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-700">
                    {task.findingExcerpt}
                  </blockquote>
                )}

                <div className="mt-3 grid gap-2 sm:grid-cols-[1fr_2fr]">
                  <select
                    value={draft.verdict}
                    onChange={(event) =>
                      updateDraft(task, {
                        verdict: event.target.value as Verdict,
                      })
                    }
                    className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
                  >
                    <option value="confirmed">确认成立</option>
                    <option value="false_positive">误报，应剔除</option>
                    <option value="need_adjustment">成立但需调级</option>
                  </select>
                  <input
                    value={draft.comment}
                    onChange={(event) =>
                      updateDraft(task, { comment: event.target.value })
                    }
                    placeholder="复核意见（会写入标注样本，说明为什么模型判断不对）"
                    className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>

                {draft.verdict === "need_adjustment" && (
                  <select
                    value={draft.level}
                    onChange={(event) =>
                      updateDraft(task, { level: event.target.value })
                    }
                    className="mt-2 rounded-lg border border-slate-300 px-3 py-2 text-sm"
                  >
                    <option value="">选择调整后的风险等级</option>
                    <option value="high">高风险</option>
                    <option value="medium">中风险</option>
                    <option value="low">低风险</option>
                    <option value="none">无风险</option>
                  </select>
                )}

                <div className="mt-3 flex justify-end">
                  <button
                    type="button"
                    disabled={
                      busyId === task.id ||
                      (draft.verdict === "need_adjustment" && !draft.level)
                    }
                    onClick={() => void submit(task)}
                    className="rounded-lg bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700 disabled:opacity-40"
                  >
                    {busyId === task.id ? "提交中…" : "提交复核结论"}
                  </button>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

const Stat = ({
  label,
  value,
  highlight,
}: {
  label: string;
  value?: string | number | null;
  highlight?: boolean;
}) => (
  <div
    className={`rounded-lg px-3 py-2 ${highlight ? "bg-orange-50 text-orange-800" : "bg-slate-50 text-slate-700"}`}
  >
    <div className="text-[11px] opacity-70">{label}</div>
    <div className="text-lg font-semibold tabular-nums">{value ?? "—"}</div>
  </div>
);

/** 后端直接返回比率小数，这里统一展示为百分数。 */
const percentOf = (value?: number | null): string | null =>
  value === null || value === undefined ? null : `${Math.round(value * 100)}%`;
