import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { getHealth } from "@/api";
import type { HealthResponse } from "@/types";

const links = [
  { to: "/", label: "合同列表", end: true },
  { to: "/upload", label: "上传审查" },
  { to: "/queue", label: "法务复核" },
  { to: "/rules", label: "合规红线" },
  { to: "/knowledge", label: "法规知识库" },
];

export default function AppShell() {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  return (
    <div className="min-h-full">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-4 px-6 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold text-slate-900">
              信贷合同合规审查
            </span>
            <span className="text-xs text-slate-400">
              规则引擎 + LLM/RAG 双通道
            </span>
          </div>

          <nav className="flex flex-wrap gap-1">
            {links.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                end={link.end}
                className={({ isActive }) =>
                  `rounded-lg px-3 py-1.5 text-sm transition ${
                    isActive
                      ? "bg-slate-900 text-white"
                      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  }`
                }
              >
                {link.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2 text-xs">
            {health ? (
              <>
                <span
                  className={`rounded-full px-2 py-0.5 font-medium ${
                    health.status === "ok"
                      ? "bg-emerald-100 text-emerald-700"
                      : "bg-amber-100 text-amber-800"
                  }`}
                >
                  {health.status === "ok" ? "链路正常" : "降级运行"}
                </span>
                <span className="text-slate-500" title="LLM 供应商">
                  LLM：{health.llmProvider}
                </span>
                <span
                  className="text-slate-500"
                  title={
                    `主审通道（环节3 合规匹配 / 环节4 风险定性）与复核通道（环节7）的思考深度。` +
                    `default 即沿用供应商最深档；思考型模型不支持完全关闭`
                  }
                >
                  思考：{health.llmReasoningEffort}
                  {health.judgeReasoningEffort
                    ? ` / 复核 ${health.judgeReasoningEffort}`
                    : " / 复核未启用"}
                </span>
                <span
                  className={
                    health.embeddingIsMock ? "text-amber-700" : "text-slate-500"
                  }
                  title={
                    health.embeddingIsMock
                      ? "当前为哈希 mock 向量，检索结果不具备语义相关性"
                      : "向量检索可用"
                  }
                >
                  向量：
                  {health.embeddingIsMock
                    ? "mock（仅链路验证）"
                    : health.embeddingProvider}
                </span>
                <span
                  className={
                    health.judgeMode === "llm" && !health.judgeIndependent
                      ? "text-amber-700"
                      : "text-slate-500"
                  }
                  title={
                    health.judgeMode === "llm"
                      ? health.judgeIndependent
                        ? "复核模型与主审来路不同，裁定构成独立信号"
                        : "回音壁：复核与主审同一个模型，只会附和；可用 JUDGE_SAME_MODEL_POLICY 改口径"
                      : health.judgeMode === "rule_check"
                        ? "主审与复核同模型，已降级为确定性规则复核（引用核验 + 双通道一致性）"
                        : "本次链路不执行结论复核"
                  }
                >
                  复核：
                  {health.judgeMode === "llm"
                    ? `${health.judgeModel}${health.judgeIndependent ? "" : "（同源）"}`
                    : health.judgeMode === "rule_check"
                      ? "规则复核"
                      : "未启用"}
                </span>
                <span
                  className={
                    health.queueState === "connected" &&
                    health.queueWorkerAlive !== false
                      ? "text-slate-500"
                      : "text-amber-700"
                  }
                  title={
                    health.queueState !== "connected"
                      ? health.queueEnabled
                        ? "队列声明启用但 Redis 不可达：审查退回 API 进程内执行，重启即丢任务"
                        : "QUEUE_ENABLED=false：审查在 API 进程内执行，重启即丢任务"
                      : health.queueWorkerAlive === false
                        ? "Redis 连通但没有 worker 心跳：审查会一直排队，请启动 `python -m app.tasks.worker`"
                        : `任务队列已连接，积压 ${health.queueDepth ?? 0} 条；审查在独立 worker 进程执行`
                  }
                >
                  {health.queueState !== "connected"
                    ? health.queueState === "in_process"
                      ? "队列：进程内（降级）"
                      : "队列：不可达"
                    : health.queueWorkerAlive === false
                      ? `worker：未运行（积压 ${health.queueDepth ?? 0}）`
                      : `队列：积压 ${health.queueDepth ?? 0}`}
                </span>
                <span className="text-slate-500">
                  法条 {health.knowledgeProvisionCount} · 红线{" "}
                  {health.redLineRuleCount}
                </span>
              </>
            ) : (
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-500">
                后端未连接
              </span>
            )}
          </div>
        </div>
        {!!health && (health.configWarnings?.length ?? 0) > 0 && (
          <div className="bg-amber-50 px-6 py-2 text-xs text-amber-800">
            <div className="font-medium">
              降级运行（{(health.configWarnings ?? []).length}{" "}
              项配置口径不该上线）
            </div>
            <ul className="mt-1 list-disc pl-5">
              {(health.configWarnings ?? []).map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
      </header>

      <main className="mx-auto max-w-[1600px] px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
