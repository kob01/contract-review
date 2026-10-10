import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { apiErrorMessage } from "@/api/client";
import {
  deleteContract,
  listContracts,
  reparseContract,
  startReview,
} from "@/api";
import { Card, Empty, ErrorNote } from "@/components/badges";
import {
  contractKindLabels,
  contractStatusLabels,
  formatBytes,
  formatTime,
} from "@/lib/labels";
import type { ContractBrief } from "@/types";

const statusTone: Record<string, string> = {
  reviewed: "bg-emerald-100 text-emerald-700",
  reviewed_pending: "bg-emerald-100 text-emerald-700",
  reviewing: "bg-blue-100 text-blue-700",
  parsed: "bg-slate-100 text-slate-700",
  failed: "bg-red-100 text-red-700",
  uploaded: "bg-amber-100 text-amber-800",
  parsing: "bg-amber-100 text-amber-800",
};

export default function ContractListPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<ContractBrief[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listContracts();
      setItems(data.items);
      setError(null);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runReview = async (contract: ContractBrief) => {
    setBusyId(contract.id);
    try {
      const review = await startReview(contract.id);
      navigate(`/contracts/${contract.id}/reviews/${review.id}`);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const reparse = async (contract: ContractBrief) => {
    setBusyId(contract.id);
    try {
      await reparseContract(contract.id);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (contract: ContractBrief) => {
    if (
      !window.confirm(
        `确认删除《${contract.title}》及其审查记录？该操作不可恢复。`,
      )
    )
      return;
    setBusyId(contract.id);
    try {
      await deleteContract(contract.id);
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-4">
      <Card
        title="合同清单"
        actions={
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">共 {items.length} 份</span>
            <button
              type="button"
              onClick={() => void refresh()}
              className="rounded-lg border border-slate-300 px-3 py-1 text-xs hover:bg-slate-50"
            >
              刷新
            </button>
            <Link
              to="/upload"
              className="rounded-lg bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700"
            >
              上传合同
            </Link>
          </div>
        }
      >
        <ErrorNote message={error} />

        {loading ? (
          <p className="text-sm text-slate-500">加载中…</p>
        ) : items.length === 0 ? (
          <Empty>
            还没有合同。上传一份消费信贷或小贷合同，系统会先抽取利率与费用指标，
            再走规则引擎与模型双通道审查。
          </Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-slate-500">
                <tr className="border-b border-slate-200">
                  <th className="py-2 pr-4">合同</th>
                  <th className="py-2 pr-4">种类</th>
                  <th className="py-2 pr-4">状态</th>
                  <th className="py-2 pr-4">规模</th>
                  <th className="py-2 pr-4">风险评分</th>
                  <th className="py-2 pr-4">待复核</th>
                  <th className="py-2 pr-4">上传时间</th>
                  <th className="py-2">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((contract) => (
                  <tr
                    key={contract.id}
                    className="border-b border-slate-100 hover:bg-slate-50/60"
                  >
                    <td className="py-2.5 pr-4">
                      <div className="font-medium text-slate-900">
                        {contract.title}
                      </div>
                      <div className="text-xs text-slate-500">
                        {contract.fileName} ·{" "}
                        {contract.lenderName ?? "未填写放款机构"}
                      </div>
                    </td>
                    <td className="py-2.5 pr-4 text-xs text-slate-600">
                      {contractKindLabels[contract.contractKind]}
                    </td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${statusTone[contract.status] ?? "bg-slate-100 text-slate-600"}`}
                      >
                        {contractStatusLabels[contract.status]}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-xs text-slate-500">
                      {contract.pageCount} 页 /{" "}
                      {formatBytes(contract.fileSizeBytes)}
                    </td>
                    <td className="py-2.5 pr-4">
                      {contract.latestRiskScore === null ||
                      contract.latestRiskScore === undefined ? (
                        <span className="text-xs text-slate-400">未审查</span>
                      ) : (
                        <span className="font-semibold tabular-nums text-slate-800">
                          {contract.latestRiskScore}
                          <span className="ml-1 text-xs font-normal text-slate-400">
                            /100
                          </span>
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 pr-4">
                      {contract.latestNeedsHumanReview ? (
                        <span className="rounded-full bg-orange-100 px-2 py-0.5 text-xs text-orange-700">
                          需要
                        </span>
                      ) : (
                        <span className="text-xs text-slate-400">—</span>
                      )}
                    </td>
                    <td className="py-2.5 pr-4 text-xs text-slate-500">
                      {formatTime(contract.createdAt)}
                    </td>
                    <td className="py-2.5">
                      <div className="flex flex-wrap gap-2 text-xs">
                        {contract.latestReviewId && (
                          <Link
                            to={`/contracts/${contract.id}/reviews/${contract.latestReviewId}`}
                            className="rounded-lg border border-slate-300 px-2 py-1 hover:bg-slate-100"
                          >
                            报告
                          </Link>
                        )}
                        <button
                          type="button"
                          disabled={
                            busyId === contract.id ||
                            contract.status === "parsing"
                          }
                          onClick={() => void runReview(contract)}
                          className="rounded-lg bg-slate-900 px-2 py-1 text-white disabled:opacity-40"
                        >
                          {contract.latestReviewId ? "重新审查" : "开始审查"}
                        </button>
                        <button
                          type="button"
                          disabled={busyId === contract.id}
                          onClick={() => void reparse(contract)}
                          className="rounded-lg border border-slate-300 px-2 py-1 hover:bg-slate-100"
                        >
                          重解析
                        </button>
                        <button
                          type="button"
                          disabled={busyId === contract.id}
                          onClick={() => void remove(contract)}
                          className="rounded-lg border border-red-300 px-2 py-1 text-red-600 hover:bg-red-50"
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p className="text-xs text-slate-400">
        说明：风险评分由结论等级折算（高 18 / 中 8 / 低 2
        分扣减），只用于横向比较，
        不构成合规结论；结论本身以规则命中与已核验引用为准。
      </p>
    </div>
  );
}
