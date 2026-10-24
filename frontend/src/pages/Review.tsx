import { useState, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { Trans, useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getDecisionSnapshot, getReviewQueue, postJobFeedback, type ReviewItem,
} from '../api/client'
import { Card, Chip, EmptyState, LoadingState } from '../components'
import { findPipelineStage } from '../config/pipelineStages'
import { DecisionPanel } from './JobDetail'

type Filter = 'all' | 'rejected' | 'accepted'

// Display names, not internal keys: "requirements_gate" -> "Requirements Gate".
const stageName = (key: string) => findPipelineStage(key)?.label ?? key.replaceAll('_', ' ')
// An audit stratum is "<stage>:<rule>", e.g. "requirements_gate:education" -> "Requirements Gate · education".
const stratumName = (stratum: string) => {
  const [stage, rule] = stratum.split(':', 2)
  return rule ? `${stageName(stage)} · ${rule.replaceAll('_', ' ')}` : stageName(stage)
}

const pill = (active: boolean): CSSProperties => ({
  border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
  borderRadius: 'var(--radius-pill)',
  padding: '0.25rem 0.75rem',
  fontSize: '0.8rem',
  background: active ? 'color-mix(in srgb, var(--color-accent) 13%, transparent)' : 'transparent',
  color: active ? 'var(--color-accent)' : 'var(--color-muted)',
  cursor: 'pointer',
})

function Snapshot({ decisionId }: { decisionId: string }) {
  const { t } = useTranslation()
  const { data, isError, isLoading } = useQuery({
    queryKey: ['decision-snapshot', decisionId], queryFn: () => getDecisionSnapshot(decisionId), retry: false,
  })
  if (isLoading) return <LoadingState />
  if (isError || !data) return <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>{t('review.postingUnavailable')}</div>
  return (
    <div style={{ fontSize: '0.82rem', whiteSpace: 'pre-wrap', maxHeight: '18rem', overflow: 'auto', color: 'var(--color-text)' }}>
      {data.description}
    </div>
  )
}

type Answer = 'interested' | 'not_interested' | 'unsure'

// Presentational: one queued decision with the reasons it needs a human, and the three answers.
export function ReviewCard({
  item, pending, onAnswer,
}: { item: ReviewItem; pending: boolean; onAnswer: (type: Answer) => void }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState<'none' | 'decision' | 'posting'>('none')
  const d = item.decision
  const a = d.signals?.assessment
  const audit = d.signals?.audit?.sampled ? d.signals.audit : undefined
  const rejected = d.outcome === 'rejected'
  // The rule(s) that decided: the violated evidence of the deciding stage.
  const deciding = d.evidence.filter(e => e.state === 'violated' && e.source.stage === d.deciding_stage)
  const title = d.job?.title || d.job_id
  return (
    <Card style={{ marginBottom: 'var(--space-3)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>
            {item.job_stored ? <Link to={`/jobs/${d.job_id}`}>{title}</Link> : title}
          </div>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>
            {[d.job?.company, d.job?.location, d.job?.source].filter(Boolean).join(' · ')}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
          <Chip tone={rejected ? 'error' : 'info'}>{rejected ? t('review.rejectedAt', { stage: stageName(d.deciding_stage) }) : t('review.accepted')}</Chip>
          {audit && (
            <Chip title={t('review.auditSampleTitle')}>
              {t('review.auditSample')}
            </Chip>
          )}
          {d.decision_confidence != null && (
            <Chip title={t('review.confidenceTitle')}>{t('review.confidence', { value: d.decision_confidence.toFixed(2) })}</Chip>
          )}
        </div>
      </div>
      {audit && (
        <div style={{ margin: '0.6rem 0 0', fontSize: '0.85rem' }}>
          <div style={{ color: 'var(--color-muted)' }}>
            <Trans
              i18nKey="review.auditExplanation"
              values={{ rule: stratumName(audit.stratum), count: audit.stratum_size }}
              components={{ b: <strong /> }}
            />
          </div>
          {deciding.length > 0 && (
            <ul style={{ margin: '0.4rem 0 0', paddingLeft: '1.1rem' }}>
              {deciding.map((e, i) => (
                <li key={`${e.source.rule_id}-${i}`}>
                  {e.message || e.source.rule_id}
                  {e.span?.text ? <> — “{e.span.text}”</> : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {a && a.reasons.length > 0 && (
        <ul style={{ margin: '0.6rem 0 0', paddingLeft: '1.1rem', fontSize: '0.85rem' }}>
          {a.reasons.map(r => <li key={r}>{r}</li>)}
        </ul>
      )}
      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginTop: '0.75rem' }}>
        <button type="button" disabled={pending} onClick={() => onAnswer('interested')} style={pill(true)}>
          {t('review.answers.interested')}
        </button>
        <button type="button" disabled={pending} onClick={() => onAnswer('not_interested')} style={pill(false)}>
          {t('review.answers.not_interested')}
        </button>
        <button type="button" disabled={pending} onClick={() => onAnswer('unsure')} style={pill(false)}>
          {t('review.answers.unsure')}
        </button>
        <button type="button" aria-expanded={open === 'decision'} onClick={() => setOpen(open === 'decision' ? 'none' : 'decision')} style={pill(open === 'decision')}>
          {t('review.why')}
        </button>
        {item.has_snapshot && (
          <button type="button" aria-expanded={open === 'posting'} onClick={() => setOpen(open === 'posting' ? 'none' : 'posting')} style={pill(open === 'posting')}>
            {t('review.posting')}
          </button>
        )}
      </div>
      {open === 'decision' && <div style={{ marginTop: '0.75rem' }}><DecisionPanel decision={d} /></div>}
      {open === 'posting' && <div style={{ marginTop: '0.75rem' }}><Snapshot decisionId={d.decision_id} /></div>}
    </Card>
  )
}

export default function Review() {
  const { t } = useTranslation()
  const [filter, setFilter] = useState<Filter>('all')
  const queryClient = useQueryClient()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['review-queue', filter],
    queryFn: () => getReviewQueue(filter === 'all' ? undefined : filter),
  })
  const [error, setError] = useState<string | null>(null)
  const { mutate, isPending } = useMutation({
    mutationFn: ({ item, type }: { item: ReviewItem; type: string }) =>
      postJobFeedback(item.decision.job_id, { type, reasons: [], decision_id: item.decision.decision_id }),
    onSuccess: () => { setError(null); queryClient.invalidateQueries({ queryKey: ['review-queue'] }) },
    onError: () => setError(t('review.saveError')),
  })

  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>{t('review.title')}</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        {t('review.intro')}
      </p>
      <div style={{ display: 'flex', gap: '0.4rem', margin: '0.75rem 0' }}>
        {(['all', 'rejected', 'accepted'] as Filter[]).map(f => (
          <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)} style={pill(filter === f)}>
            {t(`review.filters.${f}`)}
          </button>
        ))}
        {data && <span style={{ alignSelf: 'center', fontSize: '0.8rem', color: 'var(--color-muted)' }}>{t('review.toReview', { count: data.total })}</span>}
      </div>
      {error && <div role="alert" style={{ marginBottom: '0.6rem', color: 'var(--color-error)', fontSize: '0.85rem' }}>{error}</div>}
      {isLoading && <LoadingState />}
      {isError && <EmptyState message={t('review.unavailable')} />}
      {data && data.items.length === 0 && <EmptyState message={t('review.empty')} />}
      {data?.items.map(item => (
        <ReviewCard key={item.decision.decision_id} item={item} pending={isPending}
          onAnswer={type => mutate({ item, type })} />
      ))}
    </div>
  )
}
