import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams, Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  getJob, getJobExplanation, setJobAvailability, updateJobApplicationStatus, recordJobEvent, getFeedbackVocabulary, getJobFeedback,
  postJobFeedback, getJobDecision,
  type Job, type JobExplanation, type ExplanationReason, type FeedbackEvent, type FeedbackVocabulary, type DecisionRecord, type DecisionEvidence,
} from '../api/client'
import { Button, Card, Chip, LoadingState, useToast } from '../components'
import { daysAgo, formatDateTime, httpUrl } from '../utils'

function OverallScore({ score }: { score: number }) {
  const { t } = useTranslation()
  const color = score >= 70 ? 'var(--color-success)' : score >= 45 ? 'var(--color-warning)' : 'var(--color-muted)'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
      <span style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>{t('jobDetail.overallMatch')}</span>
      <span style={{ fontSize: '1.25rem', fontWeight: 'var(--font-weight-bold)', color }}>{t('jobDetail.scoreOutOf', { score })}</span>
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

// Factors with a translated label (jobDetail.factors.<key>); any other key shows as sent.
const KNOWN_FACTORS = new Set(['location_fit', 'domain_relevance', 'skills_match', 'seniority_match', 'experience_fit'])

export function MatchFactorsSection({ factors }: { factors: Record<string, number> | undefined | null }) {
  const { t } = useTranslation()
  const entries = Object.entries(factors ?? {}).filter(([, v]) => typeof v === 'number')
  if (entries.length === 0) return null

  return (
    <Card title={t('jobDetail.factors.title')} style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
        {entries.map(([key, value]) => {
          const pct = Math.max(0, Math.min(100, Math.round(value * 100)))
          return (
            <div key={key}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.8rem', marginBottom: '0.2rem' }}>
                <span style={{ color: 'var(--color-text)' }}>{KNOWN_FACTORS.has(key) ? t(`jobDetail.factors.${key}`) : key}</span>
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

const KNOWN_SIGNALS = new Set(['company', 'skill', 'title_keyword', 'remote'])

export function PersonalizationSection({ expl }: { expl: JobExplanation }) {
  const { t } = useTranslation()
  const signals = expl.personalization_signals ?? []
  const adjustment = expl.personalization_adjustment ?? 0

  if (signals.length === 0) {
    return (
      <Card title={t('jobDetail.personalization.title')} style={{ marginBottom: 'var(--space-4)' }}>
        <div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>
          {t('jobDetail.personalization.none')}
        </div>
      </Card>
    )
  }

  const isPositive = adjustment >= 0

  return (
    <Card
      title={t('jobDetail.personalization.title')}
      style={{ marginBottom: 'var(--space-4)' }}
      actions={
        <span style={{
          fontSize: '0.8rem',
          fontWeight: 'var(--font-weight-semibold)',
          color: isPositive ? 'var(--color-success)' : 'var(--color-error)',
        }}>
          {t('jobDetail.personalization.points', { value: `${isPositive ? '+' : ''}${(adjustment * 100).toFixed(1)}` })}
        </span>
      }
    >
      <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
        {signals.map((s, i) => (
          <li key={i} style={{ fontSize: '0.85rem', padding: '0.25rem 0', display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ color: 'var(--color-text)' }}>
              {KNOWN_SIGNALS.has(s.signal_type) ? t(`jobDetail.personalization.signals.${s.signal_type}`) : s.signal_type}: {s.value}
              <span style={{ color: 'var(--color-muted)' }}>{t('jobDetail.personalization.feedbackCount', { count: s.feedback_count })}</span>
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
  const { t } = useTranslation()
  const isRejected = expl.filter_result === 'rejected'
  const hasReasons = (
    expl.positive_reasons.length > 0 ||
    expl.concerns.length > 0 ||
    expl.rejection_reasons.length > 0
  )

  return (
    <Card
      style={{ marginBottom: 'var(--space-4)' }}
      title={isRejected ? t('jobDetail.explanation.whyRejected') : t('jobDetail.explanation.whyThisJob')}
      actions={
        <span style={{
          fontSize: '0.8rem',
          fontWeight: 'var(--font-weight-semibold)',
          padding: '0.2rem 0.65rem',
          borderRadius: 'var(--radius-pill)',
          background: isRejected ? 'color-mix(in srgb, var(--color-error) 13%, transparent)' : 'color-mix(in srgb, var(--color-success) 13%, transparent)',
          color: isRejected ? 'var(--color-error)' : 'var(--color-success)',
          border: `1px solid color-mix(in srgb, ${isRejected ? 'var(--color-error)' : 'var(--color-success)'} 27%, transparent)`,
        }}>
          {isRejected ? t('jobDetail.explanation.rejected') : t('jobDetail.explanation.accepted')}
        </span>
      }
    >
      {hasReasons ? (
        <>
          {expl.positive_reasons.length > 0 && (
            <ReasonList title={t('jobDetail.explanation.positive')} reasons={expl.positive_reasons} />
          )}
          {expl.concerns.length > 0 && (
            <ReasonList title={t('jobDetail.explanation.concerns')} reasons={expl.concerns} />
          )}
          {expl.rejection_reasons.length > 0 && (
            <ReasonList title={t('jobDetail.explanation.rejections')} reasons={expl.rejection_reasons} />
          )}
        </>
      ) : (
        <div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>
          {t('jobDetail.explanation.unknown')}
        </div>
      )}
    </Card>
  )
}

// Labels: jobDetail.applicationStatus.<value> ("none" for no status).
const APPLICATION_STATUSES: { value: string | null; color: string }[] = [
  { value: null,        color: 'var(--color-muted)' },
  { value: 'to_apply',  color: 'var(--color-accent)' },
  { value: 'applied',   color: 'var(--color-warning)' },
  { value: 'waiting',   color: 'var(--color-warning)' },
  { value: 'interview', color: 'var(--color-success)' },
  { value: 'accepted',  color: 'var(--color-success)' },
  { value: 'rejected',  color: 'var(--color-error)' },
  { value: 'denied',    color: 'var(--color-error)' },
  { value: 'withdrawn', color: 'var(--color-muted)' },
]

function ApplicationStatusSection({ job }: { job: Job }) {
  const { t } = useTranslation()
  const statusLabel = (value: string | null) => t(`jobDetail.applicationStatus.${value ?? 'none'}`)
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
        {t('jobDetail.applicationStatus.title')}
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
                background: isActive ? `color-mix(in srgb, ${s.color} 13%, transparent)` : 'transparent',
                color: isActive ? s.color : 'var(--color-muted)',
                cursor: isActive || isPending ? 'default' : 'pointer',
                transition: 'all 0.12s',
              }}
            >
              {statusLabel(s.value)}
            </button>
          )
        })}
      </div>
      {job.application_status && (
        <div style={{ marginTop: '0.6rem', fontSize: '0.78rem', color: current.color, fontWeight: 'var(--font-weight-semibold)' }}>
          {t('jobDetail.applicationStatus.current', { status: statusLabel(current.value) })}
        </div>
      )}
    </Card>
  )
}

// Codes from the backend vocabulary with a translated label; an unknown code shows as sent.
const FEEDBACK_TYPES = new Set(['interested', 'not_interested', 'ranking_feedback', 'unsure'])
const FEEDBACK_REASONS = new Set([
  'too_far', 'too_senior', 'insufficient_skills', 'off_domain', 'salary', 'contract_type',
  'interesting_despite_low_score', 'mis_ranked', 'other',
])

// Presentational: all data and the submit action come from props (testable without a router/query client).
export function FeedbackPanel({
  vocabulary, events, pending, onSubmit,
}: {
  vocabulary: FeedbackVocabulary
  events: FeedbackEvent[]
  pending: boolean
  onSubmit: (body: { type: string; reasons: string[]; note?: string }) => Promise<unknown>
}) {
  const { t } = useTranslation()
  const typeLabel = (code: string) => (FEEDBACK_TYPES.has(code) ? t(`jobDetail.feedback.types.${code}`) : code)
  const reasonLabel = (code: string) => (FEEDBACK_REASONS.has(code) ? t(`jobDetail.feedback.reasons.${code}`) : code)
  const [type, setType] = useState<string | null>(null)
  const [reasons, setReasons] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const last = events.find(e => e.signal_class === 'explicit')
  const allowed = type ? vocabulary.allowed_reasons[type] ?? [] : []

  const toggle = (r: string) =>
    setReasons(prev => (prev.includes(r) ? prev.filter(x => x !== r) : [...prev, r]))
  const pick = (code: string) => { setType(code); setReasons([]); setError(null) }
  // Reset the form only once the server has accepted the feedback: on failure the user keeps
  // their selection and note and sees an error.
  const submit = async () => {
    if (!type) return
    try {
      await onSubmit({ type, reasons, note: note.trim() || undefined })
      setType(null); setReasons([]); setNote(''); setError(null)
    } catch {
      setError(t('jobDetail.feedback.saveError'))
    }
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

  return (
    <Card title={t('jobDetail.feedback.title')} style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
        {vocabulary.explicit_types.map(code => (
          <button key={code} type="button" aria-pressed={type === code} onClick={() => pick(code)} style={pill(type === code)}>
            {typeLabel(code)}
          </button>
        ))}
      </div>
      {type && (
        <div style={{ marginTop: '0.75rem' }}>
          <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', marginBottom: '0.4rem' }}>{t('jobDetail.feedback.why')}</div>
          <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
            {allowed.map(r => (
              <button key={r} type="button" aria-pressed={reasons.includes(r)} onClick={() => toggle(r)} style={pill(reasons.includes(r))}>
                {reasonLabel(r)}
              </button>
            ))}
          </div>
          <textarea
            aria-label={t('jobDetail.feedback.noteLabel')}
            value={note}
            maxLength={500}
            onChange={e => setNote(e.target.value)}
            placeholder={t('jobDetail.feedback.notePlaceholder')}
            style={{ width: '100%', marginTop: '0.6rem', fontSize: '0.85rem' }}
          />
          <button type="button" disabled={pending} onClick={submit} style={{ ...pill(true), marginTop: '0.5rem' }}>
            {t('jobDetail.feedback.save')}
          </button>
          {error && <div role="alert" style={{ marginTop: '0.4rem', fontSize: '0.8rem', color: 'var(--color-error)' }}>{error}</div>}
        </div>
      )}
      <div style={{ marginTop: '0.6rem', fontSize: '0.75rem', color: 'var(--color-muted)' }}>
        {t('jobDetail.feedback.localOnly')}
      </div>
      {last && (
        <div style={{ marginTop: '0.4rem', fontSize: '0.78rem', color: 'var(--color-muted)' }}>
          {t('jobDetail.feedback.last', { type: typeLabel(last.type) })}
          {last.reasons.length > 0 && t('jobDetail.feedback.lastReasons', { reasons: last.reasons.map(reasonLabel).join(', ') })}
        </div>
      )}
    </Card>
  )
}

function FeedbackSection({ jobId, decisionId }: { jobId: string; decisionId?: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data: vocabulary, isError } = useQuery({ queryKey: ['feedback-vocabulary'], queryFn: getFeedbackVocabulary })
  const { data: events } = useQuery({ queryKey: ['job-feedback', jobId], queryFn: () => getJobFeedback(jobId) })
  const { mutateAsync, isPending } = useMutation({
    // pin the decision the user is looking at, so the feedback is attributed to it even if a newer run lands meanwhile
    mutationFn: (body: { type: string; reasons: string[]; note?: string }) =>
      postJobFeedback(jobId, { ...body, decision_id: decisionId }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['job-feedback', jobId] }),
  })
  if (isError) return <Card title={t('jobDetail.feedback.title')} style={{ marginBottom: 'var(--space-4)' }}><div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>{t('jobDetail.feedback.unavailable')}</div></Card>
  if (!vocabulary) return null
  return <FeedbackPanel vocabulary={vocabulary} events={events ?? []} pending={isPending} onSubmit={mutateAsync} />
}

// Presentational: the decision record for a job — what supports it, what argues against it, what is
// UNKNOWN, what conflicts — straight from the journal (nothing recomputed in the browser).
export function DecisionPanel({ decision }: { decision: DecisionRecord }) {
  const { t } = useTranslation()
  const assessment = decision.signals?.assessment
  const by = (p: DecisionEvidence['polarity']) => decision.evidence.filter(e => e.polarity === p)
  // `title` is the already translated heading, with the count in it.
  const group = (title: string, items: DecisionEvidence[]) =>
    items.length === 0 ? null : (
      <div style={{ marginBottom: '0.7rem' }}>
        <div style={{ fontSize: '0.75rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
          {title}
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
    <Card title={t('jobDetail.decision.title')} style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ fontSize: '0.85rem', marginBottom: '0.7rem' }}>{decision.summary.headline}</div>
      {decision.decision_confidence != null && (
        <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
          {t('jobDetail.decision.confidence', { value: decision.decision_confidence.toFixed(2) })}
          {decision.confidence_calibrated ? '' : t('jobDetail.decision.notCalibrated')}
        </div>
      )}
      {assessment && assessment.state === 'review' && (
        <div role="note" style={{ fontSize: '0.82rem', color: 'var(--color-warning)', marginBottom: '0.7rem' }}>
          {decision.outcome === 'review' ? t('jobDetail.decision.flagged') : t('jobDetail.decision.wouldFlag')}:{' '}
          {assessment.reasons.join('; ')}.{' '}
          {decision.outcome === 'accepted' || decision.outcome === 'review'
            ? t('jobDetail.decision.staysVisible')
            : t('jobDetail.decision.staysRejected')}
        </div>
      )}
      {group(t('jobDetail.decision.supporting', { count: by('positive').length }), by('positive'))}
      {group(t('jobDetail.decision.against', { count: by('negative').length }), by('negative'))}
      {group(t('jobDetail.decision.unknown', { count: by('unknown').length }), by('unknown'))}
      {decision.conflicts.length > 0 && (
        <div style={{ marginBottom: '0.5rem' }}>
          <div style={{ fontSize: '0.75rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-warning)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
            {t('jobDetail.decision.conflicts', { count: decision.conflicts.length })}
          </div>
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {decision.conflicts.map((c, i) => <li key={i} style={{ fontSize: '0.82rem' }}>{c.description}</li>)}
          </ul>
        </div>
      )}
      <div style={{ fontSize: '0.72rem', color: 'var(--color-muted)' }}>
        {t('jobDetail.decision.ids', { decision: decision.decision_id.slice(0, 8), run: decision.run_id.slice(0, 8) })}
      </div>
    </Card>
  )
}

// Where the offer was seen (issue #86): every source that listed it, linked when its
// address is known, so a duplicate across France Travail and EURES shows once.
export function SeenOn({ job }: { job: Job }) {
  const { t } = useTranslation()
  const sightings = job.provenance?.length ? job.provenance : [{ source: job.source, url: null }]
  const name = (source: string) => t(`settings.sources.names.${source}`, { defaultValue: source })
  return (
    <span>
      {t('jobDetail.header.seenOn')}{' '}
      {sightings.map((p, i) => {
        const url = httpUrl(p.url)
        return (
          <span key={p.source}>
            {i > 0 && ', '}
            {url ? <a href={url} target="_blank" rel="noopener noreferrer">{name(p.source)}</a> : name(p.source)}
          </span>
        )
      })}
    </span>
  )
}

// Whether the offer is still open (issue #86): an expired offer says why, and the user can
// report an offer as no longer available, or undo that report.
export function AvailabilitySection({ job }: { job: Job }) {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { mutate, isPending } = useMutation({
    mutationFn: (available: boolean) => setJobAvailability(job.id, available),
    onSuccess: updated => {
      queryClient.setQueryData(['job', job.id], updated)
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
    onError: () => toast.show({ message: t('jobDetail.availability.failed'), tone: 'danger' }),
  })
  const expired = job.status === 'expired'
  return (
    <div
      role={expired ? 'status' : undefined}
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-3)', flexWrap: 'wrap',
        marginBottom: 'var(--space-4)', fontSize: 'var(--font-size-sm)',
        color: expired ? 'var(--color-warning)' : 'var(--color-muted)',
      }}
    >
      <span>
        {expired
          ? t(`jobDetail.availability.expired.${job.expiry_reason ?? 'not_seen'}`, {
              when: job.expired_at ? formatDateTime(job.expired_at) : '',
            })
          : job.valid_through ? t('jobDetail.availability.openUntil', { when: formatDateTime(job.valid_through) }) : null}
      </span>
      {(!expired || job.expiry_reason === 'user_report') && (
        <Button variant="ghost" disabled={isPending} onClick={() => mutate(expired)}>
          {t(expired ? 'jobDetail.availability.undo' : 'jobDetail.availability.report')}
        </Button>
      )}
    </div>
  )
}

function JobHeader({ job, overallScore }: { job: Job; overallScore: number | null }) {
  const { t } = useTranslation()
  const age = daysAgo(job.published_at)
  const originalUrl = httpUrl(job.apply_url) ?? httpUrl(job.canonical_url)

  return (
    <Card style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ marginBottom: '0.75rem' }}>
        <h1 style={{ margin: '0 0 0.35rem', fontSize: '1.35rem', fontWeight: 'var(--font-weight-bold)' }}>
          {job.title || t('jobDetail.header.noTitle')}
        </h1>
        <div style={{ fontSize: '0.88rem', color: 'var(--color-muted)' }}>
          {[job.company, job.location].filter(Boolean).join(' · ')}
          {age !== null ? t('jobDetail.header.daysAgo', { days: age }) : ''}
          {' · '}<SeenOn job={job} />
        </div>
      </div>

      {overallScore !== null && (
        <div style={{ marginBottom: '0.85rem' }}>
          <OverallScore score={overallScore} />
        </div>
      )}

      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginBottom: '1rem' }}>
        {job.remote && <Chip tone="accent">{t('jobDetail.header.remote')}</Chip>}
        {job.countries.map(c => <Chip key={c}>{c}</Chip>)}
        {job.employment_type && <Chip>{job.employment_type}</Chip>}
        {job.skills.map(sk => <Chip key={sk}>{sk}</Chip>)}
      </div>

      {(job.salary_min || job.salary_max) && (
        <div style={{ fontSize: '0.85rem', color: 'var(--color-text)', marginBottom: '0.75rem' }}>
          {t('jobDetail.header.salary', { range: [job.salary_min, job.salary_max].filter(Boolean).join(' – '), currency: job.salary_currency })}
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
              color: 'var(--color-on-accent)',
              borderRadius: 'var(--radius-md)',
              padding: '0.45rem 1rem',
              textDecoration: 'none',
              fontSize: '0.88rem',
              fontWeight: 'var(--font-weight-semibold)',
              display: 'inline-block',
            }}
          >
            {t('jobDetail.header.viewOriginal')}
          </a>
        </div>
      )}
    </Card>
  )
}

function DescriptionSection({ description }: { description: string }) {
  const { t } = useTranslation()
  return (
    <Card title={t('jobDetail.description')}>
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
  const { t } = useTranslation()
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
      <div style={{ color: 'var(--color-error)', marginBottom: '1rem' }}>{t('jobDetail.notFound')}</div>
      <Link to="/jobs" style={{ color: 'var(--color-accent)' }}>{t('jobDetail.backToJobs')}</Link>
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
        <Link to="/jobs" style={{ color: 'var(--color-muted)', textDecoration: 'none', fontSize: '0.85rem' }}>{t('jobDetail.breadcrumb')}</Link>
      </div>

      <JobHeader job={job} overallScore={overallScore} />

      <AvailabilitySection job={job} />

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
