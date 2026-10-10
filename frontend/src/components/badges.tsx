import type { ReactNode } from 'react'
import type { FindingSource, HumanReviewStatus, RiskLevel } from '@/types'
import {
  crossCheckLabels,
  humanReviewLabels,
  humanReviewTone,
  riskLabels,
  riskTone,
  sourceLabels,
  sourceTone,
} from '@/lib/labels'
import type { CrossCheckStatus } from '@/types'

const base =
  'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset'

export const RiskBadge = ({ level }: { level: RiskLevel }) => (
  <span className={`${base} ${riskTone[level]}`}>{riskLabels[level]}</span>
)

export const SourceBadge = ({ source }: { source: FindingSource }) => (
  <span className={`${base} ${sourceTone[source]} ring-transparent`}>{sourceLabels[source]}</span>
)

export const CrossCheckBadge = ({ status }: { status: CrossCheckStatus }) => (
  <span
    className={`${base} ${
      status === 'consistent'
        ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
        : status === 'rule_overrides'
          ? 'bg-red-50 text-red-700 ring-red-600/20'
          : 'bg-slate-50 text-slate-600 ring-slate-500/20'
    }`}
  >
    {crossCheckLabels[status]}
  </span>
)

export const HumanReviewBadge = ({ status }: { status: HumanReviewStatus }) => (
  <span className={`${base} ${humanReviewTone[status]} ring-transparent`}>
    {humanReviewLabels[status]}
  </span>
)

export const ConfidenceMeter = ({ value }: { value: number }) => {
  const percent = Math.round(value * 100)
  const color = value >= 0.8 ? 'bg-emerald-500' : value >= 0.6 ? 'bg-amber-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2" title={`置信度 ${percent}%`}>
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-200">
        <div className={`h-full ${color}`} style={{ width: `${percent}%` }} />
      </div>
      <span className="text-xs tabular-nums text-slate-500">{percent}%</span>
    </div>
  )
}

export const Card = ({
  title,
  actions,
  children,
  className = '',
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) => (
  <section className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}>
    {(title || actions) && (
      <header className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
        <div className="flex items-center gap-2">{actions}</div>
      </header>
    )}
    <div className="px-4 py-3">{children}</div>
  </section>
)

export const Empty = ({ children }: { children: ReactNode }) => (
  <div className="rounded-lg border border-dashed border-slate-300 px-4 py-8 text-center text-sm text-slate-500">
    {children}
  </div>
)

export const ErrorNote = ({ message }: { message?: string | null }) =>
  message ? (
    <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 ring-1 ring-inset ring-red-600/20">
      {message}
    </div>
  ) : null
