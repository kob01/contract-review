import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiErrorMessage } from "@/api/client";
import { startReview, uploadContract } from "@/api";
import { Card, ErrorNote } from "@/components/badges";
import { contractKindLabels } from "@/lib/labels";
import type { ContractKind } from "@/types";

export default function UploadPage() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<ContractKind | "">("");
  const [lenderName, setLenderName] = useState("");
  const [borrowerName, setBorrowerName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!file) {
      setError("请先选择合同文件");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const contract = await uploadContract(file, {
        title: title || undefined,
        contractKind: kind || undefined,
        lenderName: lenderName || undefined,
        borrowerName: borrowerName || undefined,
      });

      if (contract.status === "failed") {
        setError(`解析失败：${contract.parseError ?? "未知原因"}`);
        return;
      }

      // 上传即审查：解析阶段已经算出指标，直接跑双通道并跳转报告页
      const review = await startReview(contract.id);
      navigate(`/contracts/${contract.id}/reviews/${review.id}`);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
      <Card title="上传合同">
        <div className="space-y-3">
          <div
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              const dropped = event.dataTransfer.files?.[0];
              if (dropped) setFile(dropped);
            }}
            onClick={() => inputRef.current?.click()}
            className="cursor-pointer rounded-xl border-2 border-dashed border-slate-300 px-6 py-10 text-center hover:border-slate-400"
          >
            <p className="text-sm font-medium text-slate-700">
              {file ? file.name : "拖拽或点击选择合同文件"}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              支持 .pdf / .docx / .txt / .md，单文件不超过 20MB。扫描件需先
              OCR。
            </p>
            <input
              ref={inputRef}
              type="file"
              accept=".pdf,.docx,.txt,.md"
              className="hidden"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-slate-600">
              合同标题（留空取文件名）
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
              />
            </label>
            <label className="text-xs text-slate-600">
              合同种类（留空自动识别）
              <select
                value={kind}
                onChange={(event) =>
                  setKind(event.target.value as ContractKind)
                }
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
              >
                <option value="">自动识别</option>
                {Object.entries(contractKindLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs text-slate-600">
              放款机构
              <input
                value={lenderName}
                onChange={(event) => setLenderName(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
              />
            </label>
            <label className="text-xs text-slate-600">
              借款人
              <input
                value={borrowerName}
                onChange={(event) => setBorrowerName(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
              />
            </label>
          </div>

          <ErrorNote message={error} />

          <button
            type="button"
            disabled={busy}
            onClick={() => void submit()}
            className="w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {busy ? "解析并审查中，请稍候…" : "上传并开始双通道审查"}
          </button>
        </div>
      </Card>

      <Card title="审查流程与前提">
        <ol className="list-decimal space-y-2 pl-5 text-sm text-slate-600">
          <li>抽取全文文本，按条款切片并记录页码与字符偏移（供高亮定位）。</li>
          <li>
            确定性抽取利率、费用、期限、预扣金额等数值指标，保留计算过程。
          </li>
          <li>规则引擎按合规红线判定硬指标：超出即定性，模型结论不得推翻。</li>
          <li>检索法条库并将结果注入提示词，模型只可引用清单内条文。</li>
          <li>
            双通道交叉校验；引用回库核验；置信度不足或引用未核验者转人工复核。
          </li>
        </ol>
        <div className="mt-4 rounded-lg bg-amber-50 p-3 text-xs leading-relaxed text-amber-800">
          法条库默认只登记"法规名 + 条号"的占位条目，条文原文需从官方渠道导入。
          未导入原文时，相关结论会被标记"引用未核验"并强制进入人工复核，
          系统不会用记忆中的表述替代权威文本。
        </div>
      </Card>
    </div>
  );
}
