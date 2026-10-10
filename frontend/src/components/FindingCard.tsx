import { useState } from 'react'
import type { Citation, Finding } from '@/types'
import { categoryLabels, formatPercent } from '@/lib/labels'
import {
  ConfidenceMeter,
  CrossCheckBadge,
  HumanReviewBadge,
  RiskBadge,
  SourceBadge,
} from './badges'

/** 法条引用卡片：未核验与占位记录必须显眼，不能伪装成既定依据。 */
const CitationItem = ({ citation }: { citation: Citation }) => (
  <li className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm font-medium text-slate-800">
        《{citation.lawName}》{citation.articleNo ?? ''}
      </span>
      {citation.verified ? (
        <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700">
          原文已核验
        </span>
      ) : (
        <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-700">
          引用未核验
        </span>
      )}
      {citation.isSample && (
        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
          占位登记·原文未导入
        </span>
      )}
      {citation.effectiveOn && (
        <span className="text-xs text-slate-500">生效 {citation.effectiveOn}</span>
      )}
    </div>
    {citation.verifyReason && (
      <p className="mt-1 text-xs text-red-700">{citation.verifyReason}</p>
    )}
    {citation.quote && (
      <p className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-slate-600">
        {citation.quote}
      </p>
    )}
    {citation.sourceUrl && (
      <a
        className="mt-1 inline-block text-xs text-blue-600 hover:underline"
        href={citation.sourceUrl}
        target="_blank"
        rel="noreferrer"
      >
        来源链接
      </a>
    )}
  </li>
)

export default function FindingCard({
  finding,
  active,
  onSelect,
}: {
  finding: Finding
  active: boolean
  onSelect: (id: string) => void
}) {
  const [showCitations, setShowCitations] = useState(active)
  const metric = finding.metricSnapshot as {
    metricValue?: number | null
    threshold?: number | null
    thresholdDesc?: string
    derivation?: string
  }

  return (
    <article
      onClick={() => onSelect(finding.id)}
      className={`cursor-pointer rounded-xl border bg-white p-4 shadow-sm transition
        ${active ? 'border-slate-900 ring-2 ring-slate-900/10' : 'border-slate-200 hover:border-slate-400'}`}
    >
      <header className="flex flex-wrap items-center gap-2">
        <RiskBadge level={finding.riskLevel} />
        <SourceBadge source={finding.source} />
        <CrossCheckBadge status={finding.crossCheckStatus} />
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
          {categoryLabels[finding.clauseCategory]}
        </span>
        {finding.ruleCode && (
          <span className="rounded-full bg-slate-900/90 px-2 py-0.5 font-mono text-xs text-white">
            {finding.ruleCode}
          </span>
        )}
        <span className="ml-auto">
          <HumanReviewBadge status={finding.humanReviewStatus} />
        </span>
      </header>

      <h3 className="mt-3 text-sm font-semibold text-slate-900">{finding.title}</h3>

      {finding.excerpt && (
        <blockquote className="mt-2 border-l-2 border-slate-300 bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-700">
          {finding.excerpt}
          <footer className="mt-1 text-[11px] text-slate-400">
            第 {finding.pageNo ?? '?'} 页
            {finding.segmentIndex ? ` · 条款 ${finding.segmentIndex}` : ''}
            {finding.charStart === null && ' · 原文未能定位，请人工核对'}
          </footer>
        </blockquote>
      )}

      <div className="mt-3 grid gap-2 text-xs text-slate-700 sm:grid-cols-2">
        <div className="rounded-lg bg-slate-50 p-3">
          <div className="mb-1 font-semibold text-slate-500">合规分析</div>
          <p className="whitespace-pre-wrap leading-relaxed">{finding.analysis}</p>
        </div>
        <div className="rounded-lg bg-emerald-50/60 p-3">
          <div className="mb-1 font-semibold text-emerald-700">修改建议</div>
          <p className="whitespace-pre-wrap leading-relaxed">{finding.suggestion}</p>
          {finding.rewrittenText && (
            <p className="mt-2 whitespace-pre-wrap rounded bg-white/70 p-2 text-slate-700">
              建议条款：{finding.rewrittenText}
            </p>
          )}
        </div>
      </div>

      {metric.metricValue !== undefined && metric.metricValue !== null && (
        <div className="mt-3 rounded-lg border border-dashed border-slate-300 px-3 py-2 text-xs text-slate-600">
          <span className="font-semibold">规则判定依据：</span>
          实测 {formatPercent(metric.metricValue)} · {metric.thresholdDesc || '—'}
          {metric.derivation && <div className="mt-1 text-slate-500">推导：{metric.derivation}</div>}
        </div>
      )}

      <footer className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <ConfidenceMeter value={finding.confidence} />
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            setShowCitations((current) => !current)
          }}
          className="text-xs text-slate-500 underline decoration-dotted hover:text-slate-800"
        >
          {showCitations ? '收起依据' : `查看依据（${finding.citations.length}）`}
          {finding.hasUnverifiedCitation && <span className="ml-1 text-red-600">· 含未核验</span>}
        </button>
      </footer>

      {showCitations && (
        <ul className="mt-3 space-y-2">
          {finding.citations.length === 0 ? (
            <li className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              本条结论没有可展示的法规引用，属于必须人工确认的情形。
            </li>
          ) : (
            finding.citations.map((citation, index) => (
              <CitationItem key={citation.provisionId ?? `${citation.lawName}-${index}`} citation={citation} />
            ))
          )}
        </ul>
      )}
    </article>
  )
}
