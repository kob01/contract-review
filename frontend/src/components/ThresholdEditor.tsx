import { useState } from "react";
import { updateThresholds } from "@/api";
import { apiErrorMessage } from "@/api/client";
import type { ThresholdItem } from "@/types";

/**
 * 监管阈值口径编辑器。
 *
 * 单独成组件的原因：这几个数是法务按月维护的数据（LPR、成本红线、路由阈值），
 * 不该让"改一个百分数"变成改代码 + 重新发版。校验口径与后端一致：
 * ratio 一律按小数填写，越界值由后端整批拒收。
 */
export default function ThresholdEditor({
  items,
  onChanged,
}: {
  items: ThresholdItem[];
  onChanged: () => void | Promise<void>;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  // 草稿与当前值相同时不算改动：避开“拉新数据后要用 effect 重置草稿”那层绕路
  const changed = items.filter(
    (item) =>
      drafts[item.key] !== undefined && drafts[item.key] !== formatEditable(item),
  );

  const commit = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateThresholds(
        changed.map((item) => ({
          key: item.key,
          // 输入框按可读性填：ratio 允许直接写百分数（24 或 0.24 都收），
          // 但存回后端统一是小数，避免 4 倍 LPR 被写成 12.4%
          value: parseValue(item, drafts[item.key] ?? ""),
          effectiveOn: today(),
        })),
      );
      setDrafts({});
      setSavedAt(new Date().toLocaleTimeString());
      await onChanged();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  if (items.length === 0) return null;

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-slate-900">
          阈值口径（法务可自助调整，无需发版）
        </h2>
        <span className="text-xs text-slate-500">
          改完立刻对新的审查生效；历史报告仍按当时快照口径展示
        </span>
      </div>

      <ul className="mt-3 space-y-2">
        {items.map((item) => {
          const raw = drafts[item.key] ?? "";
          return (
            <li key={item.key} className="flex flex-wrap items-center gap-2">
              <label className="w-44 text-xs text-slate-700" title={item.description}>
                {item.label}
                <span className="ml-1 text-[11px] text-slate-400">{item.key}</span>
              </label>
              <input
                value={raw === "" ? formatEditable(item) : raw}
                onChange={(event) =>
                  setDrafts((current) => ({
                    ...current,
                    [item.key]: event.target.value,
                  }))
                }
                className="w-28 rounded-lg border border-slate-300 px-2 py-1 text-xs tabular-nums"
              />
              <span className="text-[11px] text-slate-500">
                {item.source === "db" ? (
                  <>
                    库内值
                    {item.updatedBy ? ` · ${item.updatedBy}` : ""}
                    {item.effectiveOn ? ` · 生效 ${item.effectiveOn}` : ""}
                  </>
                ) : (
                  "环境变量默认值"
                )}
                {` · 合法区间 (${item.min}, ${item.max}]`}
              </span>
            </li>
          );
        })}
      </ul>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={changed.length === 0 || saving}
          onClick={() => void commit()}
          className="rounded-lg bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:opacity-40"
        >
          {saving ? "保存中…" : `保存 ${changed.length} 项`}
        </button>
        {changed.length > 0 && (
          <button
            type="button"
            onClick={() => setDrafts({})}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs hover:bg-slate-50"
          >
            撤销
          </button>
        )}
        {savedAt && (
          <span className="text-xs text-emerald-700">已于 {savedAt} 保存</span>
        )}
      </div>

      {error && (
        <p className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

/** ratio 口径默认按百分数展示与录入：0.24 → "24"，符合法务的书写习惯。 */
const formatEditable = (item: ThresholdItem): string =>
  item.unit === "ratio" ? trimNumber(item.value * 100) : trimNumber(item.value);

const parseValue = (item: ThresholdItem, raw: string): number => {
  const value = Number(raw);
  if (Number.isNaN(value)) {
    throw new Error(`${item.label} 不是合法数值：${raw}`);
  }
  if (item.unit !== "ratio") return value;
  // 24 与 0.24 都收：大于 1 视为百分数，否则视为小数口径
  return value > 1 ? value / 100 : value;
};

const trimNumber = (value: number): string =>
  Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)));

const today = (): string => new Date().toISOString().slice(0, 10);
