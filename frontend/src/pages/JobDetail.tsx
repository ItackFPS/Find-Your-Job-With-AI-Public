import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams, Link } from 'react-router-dom'
import {
  getJob, getJobExplanation, updateJobApplicationStatus, recordJobEvent, getFeedbackVocabulary, getJobFeedback,
  postJobFeedback, getJobDecision,
  type Job, type JobExplanation, type ExplanationReason, type FeedbackEvent, type FeedbackVocabulary, type DecisionRecord, type DecisionEvidence,
} from '../api/client'
import { Card, Chip, LoadingState } from '../components'
import { daysAgo, httpUrl } from '../utils'

function OverallScore({ score }: { score: number }) {
  const color = score >= 70 ? 'var(--color-success)' : score >= 45 ? 'var(--color-warning)' : 'var(--color-muted)'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
      <span style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>Overall match</span>
      <span style={{ fontSize: '1.25rem', fontWeight: 'var(--font-weight-bold)', color }}>{score}/100</span>
    </div>
  )
}

function ReasonList({ title, reasons }: { title: string; reasons: ExplanationReason[] }) {
  if (reasons.length === 0) return null
  return (
    <div style={{ marginBottom: '0.85rem' }}>
      <div style={{ fontSize: '0.75rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.4rem' }}>
        {title.toUpperCase()}
      </div>
      <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
        {reasons.map((r, i) => (
          <li key={i} style={{ fontSize: '0.85rem', padding: '0.25rem 0', display: 'flex', gap: '0.5rem', alignItems: 'flex-start' }}>
            <span>{r.emoji}</span>
            <span style={{ color: 'var(--color-text)' }}>{r.text}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

const FACTOR_LABELS: Record<string, string> = {
  location_fit: 'Location',
  domain_relevance: 'Domain',
  skills_match: 'Skills',
  seniority_match: 'Seniority',
  experience_fit: 'Experience',
}

export function MatchFactorsSection({ factors }: { factors: Record<string, number> | undefined | null }) {
  const entries = Object.entries(factors ?? {}).filter(([, v]) => typeof v === 'number')
  if (entries.length === 0) return null

  return (
    <Card title="Match factors" style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
        {entries.map(([key, value]) => {
          const pct = Math.max(0, Math.min(100, Math.round(value * 100)))
          return (
            <div key={key}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8rem', marginBottom: '0.2rem' }}>
                <span style={{ color: 'var(--color-text)' }}>{FACTOR_LABELS[key] ?? key}</span>
                <span style={{ color: 'var(--color-muted)' }}>{pct}%</span>
              </div>
              <div style={{ height: '6px', borderRadius: 'var(--radius-pill)', background: 'var(--color-border)', overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${pct}%`, background: 'var(--color-accent)' }} />
              </div>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

const SIGNAL_LABELS: Record<string, string> = {
  company: 'Company',
  skill: 'Skill',
  title_keyword: 'Title keyword',
  remote: 'Remote work',
}

export function PersonalizationSection({ expl }: { expl: JobExplanation }) {
  const signals = expl.personalization_signals ?? []
  const adjustment = expl.personalization_adjustment ?? 0

  if (signals.length === 0) {
    return (
      <Card title="Personalization" style={{ marginBottom: 'var(--space-4)' }}>
        <div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>
          No personalization applied yet. Feedback you give on a job is recorded to evaluate and
          improve the matching.
        </div>
      </Card>
    )
  }

  const isPositive = adjustment >= 0

  return (
    <Card
      title="Personalization"
      style={{ marginBottom: 'var(--space-4)' }}
      actions={
        <span style={{
          fontSize: '0.8rem',
          fontWeight: 'var(--font-weight-semibold)',
          color: isPositive ? 'var(--color-success)' : 'var(--color-error)',
        }}>
          {isPositive ? '+' : ''}{(adjustment * 100).toFixed(1)} pts
        </span>
      }
    >
      <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
        {signals.map((s, i) => (
          <li key={i} style={{ fontSize: '0.85rem', padding: '0.25rem 0', display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ color: 'var(--color-text)' }}>
              {SIGNAL_LABELS[s.signal_type] ?? s.signal_type}: {s.value}
              <span style={{ color: 'var(--color-muted)' }}> ({s.feedback_count} feedback)</span>
            </span>
            <span style={{ color: s.contribution >= 0 ? 'var(--color-success)' : 'var(--color-error)' }}>
              {s.contribution >= 0 ? '+' : ''}{(s.contribution * 100).toFixed(1)}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  )
}

function ExplanationSection({ expl }: { expl: JobExplanation }) {
  const isRejected = expl.filter_result === 'rejected'
  const hasReasons = (
    expl.positive_reasons.length > 0 ||
    expl.concerns.length > 0 ||
    expl.rejection_reasons.length > 0
  )

  return (
    <Card
      style={{ marginBottom: 'var(--space-4)' }}
      title={isRejected ? 'Why was this rejected?' : 'Why this job?'}
      actions={
        <span style={{
          fontSize: '0.8rem',
          fontWeight: 'var(--font-weight-semibold)',
          padding: '0.2rem 0.65rem',
          borderRadius: 'var(--radius-pill)',
          background: isRejected ? 'var(--color-error)22' : 'var(--color-success)22',
          color: isRejected ? 'var(--color-error)' : 'var(--color-success)',
          border: `1px solid ${isRejected ? 'var(--color-error)' : 'var(--color-success)'}44`,
        }}>
          {isRejected ? 'Rejected' : 'Accepted'}
        </span>
      }
    >
      {hasReasons ? (
        <>
          {expl.positive_reasons.length > 0 && (
            <ReasonList title="Positive matches" reasons={expl.positive_reasons} />
          )}
          {expl.concerns.length > 0 && (
            <ReasonList title="Things to check" reasons={expl.concerns} />
          )}
          {expl.rejection_reasons.length > 0 && (
            <ReasonList title="Rejection reasons" reasons={expl.rejection_reasons} />
          )}
        </>
      ) : (
        <div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>
          Unable to determine the reason.
        </div>
      )}
    </Card>
  )
}

const APPLICATION_STATUSES: { value: string | null; label: string; color: string }[] = [
  { value: null,        label: 'No status',  color: 'var(--color-muted)' },
  { value: 'to_apply',  label: 'To Apply',   color: 'var(--color-accent)' },
  { value: 'applied',   label: 'Applied',    color: 'var(--color-warning)' },
  { value: 'waiting',   label: 'Waiting',    color: 'var(--color-warning)' },
  { value: 'interview', label: 'Interview',  color: 'var(--color-success)' },
  { value: 'accepted',  label: 'Accepted',   color: 'var(--color-success)' },
  { value: 'rejected',  label: 'Rejected',   color: 'var(--color-error)' },
  { value: 'denied',    label: 'Denied',     color: 'var(--color-error)' },
  { value: 'withdrawn', label: 'Withdrawn',  color: 'var(--color-muted)' },
]

function ApplicationStatusSection({ job }: { job: Job }) {
  const queryClient = useQueryClient()
  const { mutate: setStatus, isPending } = useMutation({
    mutationFn: (status: string | null) => updateJobApplicationStatus(job.id, status),
    onSuccess: (updated) => {
      queryClient.setQueryData(['job', job.id], updated)
    },
  })

  const current = APPLICATION_STATUSES.find(s => s.value === job.application_status) ?? APPLICATION_STATUSES[0]

  return (
    <Card style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ fontSize: '0.75rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: 'var(--space-3)' }}>
        APPLICATION STATUS
      </div>
      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
        {APPLICATION_STATUSES.map(s => {
          const isActive = s.value === job.application_status
          return (
            <button
              key={String(s.value)}
              disabled={isPending || isActive}
              onClick={() => setStatus(s.value)}
              style={{
                border: `1px solid ${isActive ? s.color : 'var(--color-border)'}`,
                borderRadius: 'var(--radius-pill)',
                padding: '0.25rem 0.75rem',
                fontSize: '0.8rem',
                fontWeight: isActive ? 'var(--font-weight-bold)' : 'var(--font-weight-normal)',
                background: isActive ? `${s.color}22` : 'transparent',
                color: isActive ? s.color : 'var(--color-muted)',
                cursor: isActive || isPending ? 'default' : 'pointer',
                transition: 'all 0.12s',
              }}
            >
              {s.label}
            </button>
          )
        })}
      </div>
      {job.application_status && (
        <div style={{ marginTop: '0.6rem', fontSize: '0.78rem', color: current.color, fontWeight: 'var(--font-weight-semibold)' }}>
          Current: {current.label}
        </div>
      )}
    </Card>
  )
}

const FEEDBACK_TYPE_LABELS: Record<string, string> = {
  interested: 'Interested',
  not_interested: 'Not interested',
  ranking_feedback: 'Mis-ranked',
  unsure: 'Not sure',
}

const FEEDBACK_REASON_LABELS: Record<string, string> = {
  too_far: 'Too far',
  too_senior: 'Too senior',
  insufficient_skills: 'Insufficient skills',
  off_domain: 'Off-domain',
  salary: 'Salary',
  contract_type: 'Contract type',
  interesting_despite_low_score: 'Interesting despite low score',
  mis_ranked: 'Mis-ranked',
  other: 'Other',
}

// Presentational: all data and the submit action come from props (testable without a router/query client).
export function FeedbackPanel({
  vocabulary, events, pending, onSubmit,
}: {
  vocabulary: FeedbackVocabulary
  events: FeedbackEvent[]
  pending: boolean
  onSubmit: (body: { type: string; reasons: string[]; note?: string }) => Promise<unknown>
}) {
  const [type, setType] = useState<string | null>(null)
  const [reasons, setReasons] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const last = events.find(e => e.signal_class === 'explicit')
  const allowed = type ? vocabulary.allowed_reasons[type] ?? [] : []

  const toggle = (r: string) =>
    setReasons(prev => (prev.includes(r) ? prev.filter(x => x !== r) : [...prev, r]))
  const pick = (t: string) => { setType(t); setReasons([]); setError(null) }
  // Reset the form only once the server has accepted the feedback: on failure the user keeps
  // their selection and note and sees an error.
  const submit = async () => {
    if (!type) return
    try {
      await onSubmit({ type, reasons, note: note.trim() || undefined })
      setType(null); setReasons([]); setNote(''); setError(null)
    } catch {
      setError('Could not save your feedback. Please try again.')
    }
  }
  const pill = (active: boolean): CSSProperties => ({
    border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
    borderRadius: 'var(--radius-pill)',
    padding: '0.25rem 0.75rem',
    fontSize: '0.8rem',
    background: active ? 'var(--color-accent)22' : 'transparent',
    color: active ? 'var(--color-accent)' : 'var(--color-muted)',
    cursor: 'pointer',
  })

  return (
    <Card title="Your feedback" style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
        {vocabulary.explicit_types.map(t => (
          <button key={t} type="button" aria-pressed={type === t} onClick={() => pick(t)} style={pill(type === t)}>
            {FEEDBACK_TYPE_LABELS[t] ?? t}
          </button>
        ))}
      </div>
      {type && (
        <div style={{ marginTop: '0.75rem' }}>
          <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', marginBottom: '0.4rem' }}>Why? (optional)</div>
          <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
            {allowed.map(r => (
              <button key={r} type="button" aria-pressed={reasons.includes(r)} onClick={() => toggle(r)} style={pill(reasons.includes(r))}>
                {FEEDBACK_REASON_LABELS[r] ?? r}
              </button>
            ))}
          </div>
          <textarea
            aria-label="Feedback note"
            value={note}
            maxLength={500}
            onChange={e => setNote(e.target.value)}
            placeholder="Add a note (optional)"
            style={{ width: '100%', marginTop: '0.6rem', fontSize: '0.85rem' }}
          />
          <button type="button" disabled={pending} onClick={submit} style={{ ...pill(true), marginTop: '0.5rem' }}>
            Save feedback
          </button>
          {error && <div role="alert" style={{ marginTop: '0.4rem', fontSize: '0.8rem', color: 'var(--color-error)' }}>{error}</div>}
        </div>
      )}
      <div style={{ marginTop: '0.6rem', fontSize: '0.75rem', color: 'var(--color-muted)' }}>
        Recorded locally to evaluate and improve matching — it does not change your ranking yet.
      </div>
      {last && (
        <div style={{ marginTop: '0.4rem', fontSize: '0.78rem', color: 'var(--color-muted)' }}>
          Last: {FEEDBACK_TYPE_LABELS[last.type] ?? last.type}
          {last.reasons.length > 0 && <> — {last.reasons.map(r => FEEDBACK_REASON_LABELS[r] ?? r).join(', ')}</>}
        </div>
      )}
    </Card>
  )
}

function FeedbackSection({ jobId, decisionId }: { jobId: string; decisionId?: string }) {
  const queryClient = useQueryClient()
  const { data: vocabulary, isError } = useQuery({ queryKey: ['feedback-vocabulary'], queryFn: getFeedbackVocabulary })
  const { data: events } = useQuery({ queryKey: ['job-feedback', jobId], queryFn: () => getJobFeedback(jobId) })
  const { mutateAsync, isPending } = useMutation({
    // pin the decision the user is looking at, so the feedback is attributed to it even if a newer run lands meanwhile
    mutationFn: (body: { type: string; reasons: string[]; note?: string }) =>
      postJobFeedback(jobId, { ...body, decision_id: decisionId }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['job-feedback', jobId] }),
  })
  if (isError) return <Card title="Your feedback" style={{ marginBottom: 'var(--space-4)' }}><div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>Feedback is currently unavailable.</div></Card>
  if (!vocabulary) return null
  return <FeedbackPanel vocabulary={vocabulary} events={events ?? []} pending={isPending} onSubmit={mutateAsync} />
}

// Presentational: the decision record for a job — what supports it, what argues against it, what is
// UNKNOWN, what conflicts — straight from the journal (nothing recomputed in the browser).
export function DecisionPanel({ decision }: { decision: DecisionRecord }) {
  const assessment = decision.signals?.assessment
  const by = (p: DecisionEvidence['polarity']) => decision.evidence.filter(e => e.polarity === p)
  const group = (title: string, items: DecisionEvidence[]) =>
    items.length === 0 ? null : (
      <div style={{ marginBottom: '0.7rem' }}>
        <div style={{ fontSize: '0.75rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
          {title.toUpperCase()} ({items.length})
        </div>
        <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
          {items.map((e, i) => (
            <li key={`${e.source.rule_id}-${i}`} style={{ fontSize: '0.82rem', padding: '0.15rem 0' }}>
              <span style={{ color: 'var(--color-muted)' }}>[{e.dimension}]</span> {e.message}
              {e.span && e.span.text && <span style={{ color: 'var(--color-muted)' }}> — “{e.span.text}”</span>}
            </li>
          ))}
        </ul>
      </div>
    )
  return (
    <Card title="Decision record" style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ fontSize: '0.85rem', marginBottom: '0.7rem' }}>{decision.summary.headline}</div>
      {decision.decision_confidence != null && (
        <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
          Decision confidence {decision.decision_confidence.toFixed(2)}
          {decision.confidence_calibrated ? '' : ' (agreement-based, not calibrated)'}
        </div>
      )}
      {assessment && assessment.state === 'review' && (
        <div role="note" style={{ fontSize: '0.82rem', color: 'var(--color-warning)', marginBottom: '0.7rem' }}>
          {decision.outcome === 'review' ? 'Flagged for review' : 'Would be flagged for review (shadow mode)'}:{' '}
          {assessment.reasons.join('; ')}.{' '}
          {decision.outcome === 'accepted' || decision.outcome === 'review'
            ? 'The job stays ranked and visible.'
            : 'The job remains rejected; this only queues it for a human look.'}
        </div>
      )}
      {group('Supporting', by('positive'))}
      {group('Against', by('negative'))}
      {group('Unknown', by('unknown'))}
      {decision.conflicts.length > 0 && (
        <div style={{ marginBottom: '0.5rem' }}>
          <div style={{ fontSize: '0.75rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-warning)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
            CONFLICTS ({decision.conflicts.length})
          </div>
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {decision.conflicts.map((c, i) => <li key={i} style={{ fontSize: '0.82rem' }}>{c.description}</li>)}
          </ul>
        </div>
      )}
      <div style={{ fontSize: '0.72rem', color: 'var(--color-muted)' }}>
        Decision {decision.decision_id.slice(0, 8)} · run {decision.run_id.slice(0, 8)}
      </div>
    </Card>
  )
}

function JobHeader({ job, overallScore }: { job: Job; overallScore: number | null }) {
  const age = daysAgo(job.published_at)
  const originalUrl = httpUrl(job.apply_url) ?? httpUrl(job.canonical_url)

  return (
    <Card style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ marginBottom: '0.75rem' }}>
        <h1 style={{ margin: '0 0 0.35rem', fontSize: '1.35rem', fontWeight: 'var(--font-weight-bold)' }}>
          {job.title || '(no title)'}
        </h1>
        <div style={{ fontSize: '0.88rem', color: 'var(--color-muted)' }}>
          {[job.company, job.location].filter(Boolean).join(' · ')}
          {age !== null ? ` · ${age}d ago` : ''}
          {' · '}{job.source}
        </div>
      </div>

      {overallScore !== null && (
        <div style={{ marginBottom: '0.85rem' }}>
          <OverallScore score={overallScore} />
        </div>
      )}

      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginBottom: '1rem' }}>
        {job.remote && <Chip tone="accent">Remote</Chip>}
        {job.countries.map(c => <Chip key={c}>{c}</Chip>)}
        {job.employment_type && <Chip>{job.employment_type}</Chip>}
        {job.skills.map(sk => <Chip key={sk}>{sk}</Chip>)}
      </div>

      {(job.salary_min || job.salary_max) && (
        <div style={{ fontSize: '0.85rem', color: 'var(--color-text)', marginBottom: '0.75rem' }}>
          Salary: {[job.salary_min, job.salary_max].filter(Boolean).join(' – ')} {job.salary_currency}
        </div>
      )}

      {originalUrl && (
        <div>
          <a
            href={originalUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => recordJobEvent(job.id, 'apply_link_opened')}
            style={{
              background: 'var(--color-accent)',
              color: '#fff',
              borderRadius: 'var(--radius-md)',
              padding: '0.45rem 1rem',
              textDecoration: 'none',
              fontSize: '0.88rem',
              fontWeight: 'var(--font-weight-semibold)',
              display: 'inline-block',
            }}
          >
            View original offer →
          </a>
        </div>
      )}
    </Card>
  )
}

function DescriptionSection({ description }: { description: string }) {
  return (
    <Card title="Description">
      <div style={{
        fontSize: '0.85rem',
        lineHeight: '1.65',
        color: 'var(--color-muted)',
        whiteSpace: 'pre-wrap',
        maxHeight: '500px',
        overflow: 'auto',
        marginTop: 'var(--space-3)',
      }}>
        {description}
      </div>
    </Card>
  )
}

export default function JobDetail() {
  const { id } = useParams<{ id: string }>()

  const { data: job, isLoading: jobLoading, error: jobError } = useQuery({
    queryKey: ['job', id],
    queryFn: () => getJob(id!),
    enabled: !!id,
  })

  const { data: decision } = useQuery({
    queryKey: ['job-decision', id],
    queryFn: () => getJobDecision(id!),
    enabled: !!id,
  })

  const { data: expl, isLoading: explLoading } = useQuery({
    queryKey: ['job-explanation', id],
    queryFn: () => getJobExplanation(id!),
    enabled: !!id,
  })

  // I18 outcome tracking: record a "viewed" event once per job load. A ref
  // (not query state) guards against React re-render/StrictMode replay
  // firing it twice for the same id — best-effort, never blocks rendering.
  const viewedRef = useRef<string | null>(null)
  useEffect(() => {
    if (job && viewedRef.current !== job.id) {
      viewedRef.current = job.id
      recordJobEvent(job.id, 'viewed')
    }
  }, [job])

  if (jobLoading) return <LoadingState />
  if (jobError) return (
    <div style={{ padding: '2rem' }}>
      <div style={{ color: 'var(--color-error)', marginBottom: '1rem' }}>Job not found.</div>
      <Link to="/jobs" style={{ color: 'var(--color-accent)' }}>← Back to jobs</Link>
    </div>
  )
  if (!job) return null

  const overallScore = expl?.overall_score ?? (
    job.score?.final_score !== null && job.score?.final_score !== undefined
      ? Math.round(job.score.final_score * 100)
      : null
  )

  return (
    <div>
      <div style={{ marginBottom: '1rem' }}>
        <Link to="/jobs" style={{ color: 'var(--color-muted)', textDecoration: 'none', fontSize: '0.85rem' }}>← Jobs</Link>
      </div>

      <JobHeader job={job} overallScore={overallScore} />

      <ApplicationStatusSection job={job} />

      <FeedbackSection jobId={job.id} decisionId={decision?.decision_id} />

      {decision && <DecisionPanel decision={decision} />}

      {!explLoading && expl && <ExplanationSection expl={expl} />}

      {!explLoading && expl && <MatchFactorsSection factors={expl.score?.factors} />}

      {!explLoading && expl && <PersonalizationSection expl={expl} />}

      {job.description && <DescriptionSection description={job.description} />}
    </div>
  )
}
