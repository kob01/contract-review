import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { getHealth } from '@/api'
import type { HealthResponse } from '@/types'

const links = [
  { to: '/', label: '合同列表', end: true },
  { to: '/upload', label: '上传审查' },
  { to: '/queue', label: '法务复核' },
  { to: '/rules', label: '合规红线' },
  { to: '/knowledge', label: '法规知识库' },
]

export default function AppShell() {
  const [health, setHealth] = useState<HealthResponse | null>(null)

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() => setHealth(null))
  }, [])

  return (
    <div className="min-h-full">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-4 px-6 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold text-slate-900">信贷合同合规审查</span>
            <span className="text-xs text-slate-400">规则引擎 + LLM/RAG 双通道</span>
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
                      ? 'bg-slate-900 text-white'
                      : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
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
                    health.status === 'ok'
                      ? 'bg-emerald-100 text-emerald-700'
                      : 'bg-amber-100 text-amber-800'
                  }`}
                >
                  {health.status === 'ok' ? '链路正常' : '降级运行'}
                </span>
                <span className="text-slate-500" title="LLM 供应商">
                  LLM：{health.llmProvider}
                </span>
                <span
                  className={health.embeddingIsMock ? 'text-amber-700' : 'text-slate-500'}
                  title={
                    health.embeddingIsMock
                      ? '当前为哈希 mock 向量，检索结果不具备语义相关性'
                      : '向量检索可用'
                  }
                >
                  向量：{health.embeddingIsMock ? 'mock（仅链路验证）' : health.embeddingProvider}
                </span>
                <span className="text-slate-500">
                  法条 {health.knowledgeProvisionCount} · 红线 {health.redLineRuleCount}
                </span>
              </>
            ) : (
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-500">
                后端未连接
              </span>
            )}
          </div>
        </div>
        {health?.embeddingIsMock && (
          <div className="bg-amber-50 px-6 py-2 text-xs text-amber-800">
            当前使用离线 mock 向量与/或离线模型，仅可用于验证链路，产出结论不得对外使用。
            配置真实 LLM 与 Embedding 密钥、并把 RAG_USE_MOCK_EMBEDDING 设为 false 后方可用于审查。
          </div>
        )}
      </header>

      <main className="mx-auto max-w-[1600px] px-6 py-6">
        <Outlet />
      </main>
    </div>
  )
}
