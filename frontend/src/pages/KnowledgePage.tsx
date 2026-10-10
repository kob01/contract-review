import { useCallback, useEffect, useState } from 'react'
import { apiErrorMessage } from '@/api/client'
import { importProvision, knowledgeStats, listProvisions, searchProvisions } from '@/api'
import { Card, Empty, ErrorNote } from '@/components/badges'
import type { LegalProvision } from '@/types'

export default function KnowledgePage() {
  const [provisions, setProvisions] = useState<LegalProvision[]>([])
  const [stats, setStats] = useState<Record<string, string | number | boolean>>({})
  const [query, setQuery] = useState('')
  const [searchResult, setSearchResult] = useState<{ items: LegalProvision[]; matchKinds: string[]; embeddingIsMock: boolean } | null>(null)
  const [onlyUnimported, setOnlyUnimported] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [form, setForm] = useState({
    lawName: '',
    articleNo: '',
    content: '',
    sourceUrl: '',
    docType: 'department_rule',
  })

  const refresh = useCallback(async () => {
    try {
      const [list, nextStats] = await Promise.all([
        listProvisions({ onlyUnimported }),
        knowledgeStats(),
      ])
      setProvisions(list)
      setStats(nextStats)
      setError(null)
    } catch (err) {
      setError(apiErrorMessage(err))
    }
  }, [onlyUnimported])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const submit = async () => {
    setNotice(null)
    try {
      await importProvision(form)
      setNotice(`已写入《${form.lawName}》${form.articleNo}，此后引用该条文的结论会标记为已核验。`)
      setForm((current) => ({ ...current, content: '', sourceUrl: '' }))
      await refresh()
    } catch (err) {
      setError(apiErrorMessage(err))
    }
  }

  const search = async () => {
    if (query.trim().length < 2) return
    try {
      setSearchResult(await searchProvisions(query))
      setError(null)
    } catch (err) {
      setError(apiErrorMessage(err))
    }
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(420px,520px)]">
      <div className="space-y-4">
        <Card title="知识库状态">
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Stat label="条文总数" value={stats.provisionTotal} />
            <Stat label="已导入原文" value={stats.provisionImported} good />
            <Stat label="待导入占位" value={stats.provisionPlaceholder} warn />
            <Stat label="监管罚单" value={stats.caseTotal} />
          </div>
          <p className="mt-3 text-xs leading-relaxed text-slate-500">
            系统内置的只是"法规名 + 条号"的占位登记，不含条文原文。
            引用到占位条文的结论会被标记<strong className="text-amber-700">引用未核验</strong>
            并强制进入人工复核队列。请把官方发布文本逐条导入，而不是让模型补全。
          </p>
        </Card>

        <Card
          title={`待导入条文（${provisions.length}）`}
          actions={
            <label className="flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={onlyUnimported}
                onChange={(event) => setOnlyUnimported(event.target.checked)}
              />
              只看未导入原文
            </label>
          }
        >
          <ErrorNote message={error} />
          {provisions.length === 0 ? (
            <Empty>没有待导入的条文，或全部条文都已有权威原文。</Empty>
          ) : (
            <ul className="max-h-[52vh] space-y-1.5 overflow-auto pr-1 text-sm">
              {provisions.map((item) => (
                <li
                  key={item.id}
                  className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2"
                >
                  <span className="text-slate-800">
                    《{item.lawName}》<span className="text-slate-500">{item.articleNo}</span>
                  </span>
                  <span className="ml-auto text-[11px] text-slate-400">
                    {item.docType}
                    {item.isSample ? ' · 占位' : ' · 已导入'}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      setForm((current) => ({
                        ...current,
                        lawName: item.lawName,
                        articleNo: item.articleNo,
                        docType: item.docType,
                      }))
                    }
                    className="rounded-lg border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
                  >
                    填入表单
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <div className="space-y-4">
        <Card title="导入条文原文">
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <input
                value={form.lawName}
                onChange={(event) => setForm({ ...form, lawName: event.target.value })}
                placeholder="法规名称（全称）"
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
              <input
                value={form.articleNo}
                onChange={(event) => setForm({ ...form, articleNo: event.target.value })}
                placeholder="条号，如 第六百七十条"
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <select
              value={form.docType}
              onChange={(event) => setForm({ ...form, docType: event.target.value })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="law">法律</option>
              <option value="admin_regulation">行政法规</option>
              <option value="department_rule">部门规章</option>
              <option value="judicial_interpretation">司法解释</option>
              <option value="regulatory_guide">监管指引/通知</option>
            </select>
            <textarea
              value={form.content}
              onChange={(event) => setForm({ ...form, content: event.target.value })}
              rows={7}
              placeholder="粘贴官方发布的条文原文（不要使用记忆中的表述）"
              className="w-full rounded-lg border border-slate-300 p-3 text-sm outline-none focus:border-slate-900"
            />
            <input
              value={form.sourceUrl}
              onChange={(event) => setForm({ ...form, sourceUrl: event.target.value })}
              placeholder="来源链接（npc.gov.cn / gov.cn / 金融监管总局等）"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
            <button
              type="button"
              disabled={!form.lawName || !form.articleNo || form.content.trim().length < 10}
              onClick={() => void submit()}
              className="w-full rounded-lg bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700 disabled:opacity-40"
            >
              写入知识库并生成向量
            </button>
            {notice && (
              <p className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700">{notice}</p>
            )}
          </div>
        </Card>

        <Card title="检索自检">
          <div className="flex gap-2">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && void search()}
              placeholder="如：预先扣除利息 / 年化利率明示 / 格式条款无效"
              className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
            <button
              type="button"
              onClick={() => void search()}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
            >
              检索
            </button>
          </div>

          {searchResult && (
            <div className="mt-3 space-y-2">
              <p className="text-xs text-slate-500">
                召回路径：{searchResult.matchKinds.join(' / ') || '无'}
                {searchResult.embeddingIsMock && (
                  <span className="ml-2 text-amber-700">
                    · 当前为 mock 向量，相似度不代表语义相关性
                  </span>
                )}
              </p>
              {searchResult.items.length === 0 ? (
                <Empty>没有召回结果。先导入条文原文，或换用更具体的法规名/条号。</Empty>
              ) : (
                searchResult.items.map((item) => (
                  <div key={item.id} className="rounded-lg border border-slate-200 p-2 text-xs">
                    <div className="font-medium text-slate-800">
                      《{item.lawName}》{item.articleNo}
                    </div>
                    <p className="mt-1 text-slate-600">{item.content}</p>
                  </div>
                ))
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

const Stat = ({
  label,
  value,
  good,
  warn,
}: {
  label: string
  value?: string | number | boolean
  good?: boolean
  warn?: boolean
}) => (
  <div
    className={`rounded-lg px-3 py-2 ${
      good ? 'bg-emerald-50 text-emerald-700' : warn ? 'bg-amber-50 text-amber-800' : 'bg-slate-50 text-slate-700'
    }`}
  >
    <div className="text-[11px] opacity-70">{label}</div>
    <div className="text-lg font-semibold tabular-nums">{value ?? '—'}</div>
  </div>
)
