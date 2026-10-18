import { useState, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getDecisionSnapshot, getReviewQueue, postJobFeedback, type ReviewItem,
} from '../api/client'
import { Card, Chip, EmptyState, LoadingState } from '../components'
import { DecisionPanel } from './JobDetail'

type Filter = 'all' | 'rejected' | 'accepted'

const pill = (active: boolean): CSSProperties => ({
  border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
  borderRadius: 'var(--radius-pill)',
  padding: '0.25rem 0.75rem',
  fontSize: '0.8rem',
  background: active ? 'var(--color-accent)22' : 'transparent',
  color: active ? 'var(--color-accent)' : 'var(--color-muted)',
  cursor: 'pointer',
})

function Snapshot({ decisionId }: { decisionId: string }) {
  const { data, isError, isLoading } = useQuery({
    queryKey: ['decision-snapshot', decisionId], queryFn: () => getDecisionSnapshot(decisionId), retry: false,
  })
  if (isLoading) return <LoadingState />
  if (isError || !data) return <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>The posting text is no longer available.</div>
  return (
    <div style={{ fontSize: '0.82rem', whiteSpace: 'pre-wrap', maxHeight: '18rem', overflow: 'auto', color: 'var(--color-text)' }}>
      {data.description}
    </div>
  )
}

// Presentational: one queued decision with the reasons it needs a human, and the two answers.
export function ReviewCard({
  item, pending, onAnswer,
}: { item: ReviewItem; pending: boolean; onAnswer: (type: 'interested' | 'not_interested') => void }) {
  const [open, setOpen] = useState<'none' | 'decision' | 'posting'>('none')
  const d = item.decision
  const a = d.signals?.assessment
  const rejected = d.outcome === 'rejected'
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
          <Chip tone={rejected ? 'error' : 'info'}>{rejected ? `Rejected at ${d.deciding_stage}` : 'Accepted'}</Chip>
          {d.decision_confidence != null && (
            <Chip title="Agreement between signals — not calibrated">confidence {d.decision_confidence.toFixed(2)}</Chip>
          )}
        </div>
      </div>
      {a && a.reasons.length > 0 && (
        <ul style={{ margin: '0.6rem 0 0', paddingLeft: '1.1rem', fontSize: '0.85rem' }}>
          {a.reasons.map(r => <li key={r}>{r}</li>)}
        </ul>
      )}
      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginTop: '0.75rem' }}>
        <button type="button" disabled={pending} onClick={() => onAnswer('interested')} style={pill(true)}>
          I want this job
        </button>
        <button type="button" disabled={pending} onClick={() => onAnswer('not_interested')} style={pill(false)}>
          Not for me
        </button>
        <button type="button" aria-expanded={open === 'decision'} onClick={() => setOpen(open === 'decision' ? 'none' : 'decision')} style={pill(open === 'decision')}>
          Why?
        </button>
        {item.has_snapshot && (
          <button type="button" aria-expanded={open === 'posting'} onClick={() => setOpen(open === 'posting' ? 'none' : 'posting')} style={pill(open === 'posting')}>
            Posting
          </button>
        )}
      </div>
      {open === 'decision' && <div style={{ marginTop: '0.75rem' }}><DecisionPanel decision={d} /></div>}
      {open === 'posting' && <div style={{ marginTop: '0.75rem' }}><Snapshot decisionId={d.decision_id} /></div>}
    </Card>
  )
}

export default function Review() {
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
    onError: () => setError('Could not save your answer. Please try again.'),
  })

  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>Review</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        Decisions the system was not sure about — signals disagree, evidence is missing, or your own feedback
        contradicts them. Rejected jobs are listed too, so a doubtful rejection is never silent. Your answer is
        recorded as feedback; it does not change rankings by itself.
      </p>
      <div style={{ display: 'flex', gap: '0.4rem', margin: '0.75rem 0' }}>
        {(['all', 'rejected', 'accepted'] as Filter[]).map(f => (
          <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)} style={pill(filter === f)}>
            {f === 'all' ? 'All' : f === 'rejected' ? 'Rejected' : 'Accepted'}
          </button>
        ))}
        {data && <span style={{ alignSelf: 'center', fontSize: '0.8rem', color: 'var(--color-muted)' }}>{data.total} to review</span>}
      </div>
      {error && <div role="alert" style={{ marginBottom: '0.6rem', color: 'var(--color-error)', fontSize: '0.85rem' }}>{error}</div>}
      {isLoading && <LoadingState />}
      {isError && <EmptyState message="The review queue is unavailable." />}
      {data && data.items.length === 0 && <EmptyState message="Nothing to review." />}
      {data?.items.map(item => (
        <ReviewCard key={item.decision.decision_id} item={item} pending={isPending}
          onAnswer={type => mutate({ item, type })} />
      ))}
    </div>
  )
}
