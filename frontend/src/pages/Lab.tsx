import { useQuery } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import {
  getActiveStrategy, getEvalRuns, getLabExperiments, getLabMemory, getSelfCheck, type ActiveStrategy,
  type EvalRunSummary, type LabExperiment, type LabMemoryItem, type SelfCheckFeedback, type SelfCheckResponse,
  type SelfCheckStatus,
} from '../api/client'
import { Card, Chip, EmptyState, LoadingState, type ChipTone } from '../components'

const pct = (v: number | null | undefined) => (v == null ? '–' : `${Math.round(v * 1000) / 10}%`)
const signed = (v: number | null | undefined, asPct = false) =>
  v == null ? '–' : `${v > 0 ? '+' : ''}${asPct ? pct(v) : v}`

const VERDICT_TONE: Record<string, ChipTone> = {
  improvement: 'success', regression: 'error', inconclusive: 'warning', insufficient_evidence: 'default',
}

// Labels and hover texts: lab.nextAction.<code>.label/.title
const NEXT_ACTION_TONE: Record<string, ChipTone> = {
  submit_for_human_approval: 'info', reject: 'error', investigate: 'warning', approved: 'accent',
}

// Labels and explanations: lab.status.<status>.label/.text
const STATUS_TONE: Record<SelfCheckStatus, ChipTone> = {
  pass: 'success', changed: 'warning', regression: 'error', baseline_mismatch: 'error', safety_violation: 'error',
}

const ANSWERS = new Set(['interested', 'not_interested', 'unsure', 'ranking_feedback'])

// Presentational: explicit feedback on the latest decisions. These are the user's answers, never verified
// labels: a disagreement with a decision is a lead to review, and nothing here changes the self-check status.
export function SelfCheckFeedbackView({ fb }: { fb: SelfCheckFeedback }) {
  const { t } = useTranslation()
  if (fb.status === 'unavailable') {
    return <div style={{ color: 'var(--color-muted)' }}>{t('lab.feedback.notMeasured', { why: fb.why })}</div>
  }
  const answers = Object.entries(fb.latest_answers)
  const c = fb.contradictions
  return (
    <div>
      <div>
        {t('lab.feedback.coverage', { answered: fb.jobs_with_explicit_feedback, decided: fb.decided_jobs, percent: pct(fb.coverage) })}
        {answers.length > 0 && t('lab.feedback.latestAnswers', {
          answers: answers.map(([k, n]) => t('lab.feedback.answerCount', { count: n, answer: ANSWERS.has(k) ? t(`lab.answers.${k}`) : k })).join(', '),
        })}
      </div>
      <div>
        {t('lab.feedback.disagreements', {
          rejectedInterested: c.rejected_but_interested.count, acceptedNotInterested: c.accepted_but_not_interested.count,
        })}
      </div>
      <div>{t('lab.feedback.evidence', { percent: pct(fb.explanation_coverage) })}</div>
      <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)' }}>
        {t('lab.feedback.signalsNotLabels')}
      </div>
    </div>
  )
}

// Presentational: the latest self-check (current strategy vs the recorded baseline) in plain words.
export function SelfCheckView({ data }: { data: SelfCheckResponse }) {
  const { t } = useTranslation()
  const r = data.latest
  if (!r) {
    return <EmptyState message={t('lab.selfCheck.none')} />
  }
  const st = { label: t(`lab.status.${r.status}.label`), tone: STATUS_TONE[r.status], text: t(`lab.status.${r.status}.text`) }
  return (
    <div style={{ fontSize: '0.85rem' }}>
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <Chip tone={st.tone}>{st.label}</Chip>
        <span>{st.text}</span>
      </div>
      <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)', marginTop: '0.3rem' }}>
        {t('lab.selfCheck.computed', { date: r.created_at.slice(0, 16).replace('T', ' '), generator: r.generated_by })}
      </div>
      {r.datasets.map(e => {
        const q = e.eligibility
        const flips = e.baseline.flips
        return (
          <div key={e.dataset.id} style={{ marginTop: '0.7rem' }}>
            <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>
              {e.dataset.id}@{e.dataset.version}{' '}
              <span style={{ color: 'var(--color-muted)', fontWeight: 'normal' }}>
                {t('lab.selfCheck.datasetMeta', { kind: e.dataset.kind, jobs: e.dataset.n_jobs, suitable: e.dataset.accept_labels })}
              </span>{' '}
              <Chip tone={STATUS_TONE[e.status]}>{t(`lab.status.${e.status}.label`)}</Chip>
            </div>
            <div>
              {t('lab.selfCheck.suitableRejected')}{' '}
              <strong style={{ color: e.safety.unsafe_rejections.length ? 'var(--color-error)' : undefined }}>
                {e.safety.unsafe_rejections.length}
              </strong>
              {t('lab.selfCheck.metrics', { fp: q.fp ?? '–', precision: pct(q.precision), recall: pct(q.recall) })}
              {t('lab.selfCheck.reasonCoverage', { percent: pct(e.explanations.coverage) })}
            </div>
            {flips && flips.fixed.length + flips.broken.length + flips.unjudged.length > 0 && (
              <div>
                {t('lab.selfCheck.flips', { fixed: flips.fixed.length, broken: flips.broken.length, unjudged: flips.unjudged.length })}
              </div>
            )}
            {e.baseline.status === 'missing' && <div>{t('lab.selfCheck.noBaseline')}</div>}
            <div style={{ color: 'var(--color-muted)' }}>{t('lab.selfCheck.ranking', { why: e.ranking.why })}</div>
            {e.patterns.length > 0 && (
              <ul style={{ margin: '0.3rem 0 0', paddingLeft: '1.1rem' }}>
                {e.patterns.map(p => (
                  <li key={`${p.kind}-${p.key}`}>
                    {p.count} × {p.kind.replaceAll('_', ' ')} <code>{p.key}</code> ({p.job_ids.join(', ')})
                  </li>
                ))}
              </ul>
            )}
            {e.limitations.length > 0 && (
              <ul style={{ margin: '0.3rem 0 0', paddingLeft: '1.1rem', fontSize: '0.78rem', color: 'var(--color-muted)' }}>
                {e.limitations.map(l => <li key={l}>{l}</li>)}
              </ul>
            )}
          </div>
        )
      })}
      {r.feedback && (
        <div style={{ marginTop: '0.7rem' }}>
          <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
            {t('lab.selfCheck.feedbackHeading')}
          </div>
          <SelfCheckFeedbackView fb={r.feedback} />
        </div>
      )}
      {r.recommendations.length > 0 && (
        <div style={{ marginTop: '0.7rem' }}>
          <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
            {t('lab.selfCheck.nextHeading')}
          </div>
          <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
            {r.recommendations.map(x => (
              <li key={x.text}>
                <Chip tone={x.kind === 'action' ? 'warning' : 'default'}
                  title={x.kind === 'action' ? t('lab.selfCheck.actionTitle') : t('lab.selfCheck.suggestionTitle')}>
                  {t(`lab.selfCheck.recommendationKind.${x.kind}`, { defaultValue: x.kind })}
                </Chip>{' '}
                {x.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data.history.length > 1 && (
        <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)', marginTop: '0.5rem' }}>
          {t('lab.selfCheck.earlier', { history: data.history.slice(1, 8).map(h => `${h.created_at.slice(0, 10)} ${h.status ? t(`lab.status.${h.status}.label`) : '?'}`).join(' · ') })}
        </div>
      )}
    </div>
  )
}

// Presentational: one evaluation run as a table row.
export function RunRow({ run }: { run: EvalRunSummary }) {
  const { t } = useTranslation()
  const o = run.overall
  return (
    <tr>
      <td>{run.created_at.slice(0, 16).replace('T', ' ')}</td>
      <td>{run.dataset.id}@{run.dataset.version} <span style={{ color: 'var(--color-muted)' }}>({run.dataset.kind})</span></td>
      <td title={run.strategy.fingerprint}>{run.strategy.name} · {run.strategy.fingerprint.slice(0, 8)}</td>
      <td>{run.n_labelled ?? '–'}</td>
      <td>{t('lab.runs.confusion', { tp: o.tp ?? '–', fp: o.fp ?? '–', fn: o.fn ?? '–', tn: o.tn ?? '–' })}</td>
      <td>{pct(o.precision)}</td>
      <td>{pct(o.recall)}</td>
      <td style={{ color: (o.unsafe_rejection_rate ?? 0) > 0 ? 'var(--color-error)' : undefined }}>{pct(o.unsafe_rejection_rate)}</td>
    </tr>
  )
}

// Presentational: one lab experiment with its verdict, guardrails, metric deltas and limits.
export function ExperimentCard({ exp }: { exp: LabExperiment }) {
  const { t } = useTranslation()
  const d = exp.delta ?? {}
  return (
    <Card compact style={{ marginBottom: 'var(--space-2)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap' }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>{exp.hypothesis ?? exp.candidate_id}</div>
          <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)' }}>
            {exp.experiment_id} · {exp.dataset_id} · {exp.created_at.slice(0, 16).replace('T', ' ')}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
          <Chip tone={VERDICT_TONE[exp.verdict] ?? 'default'}>{t(`lab.verdicts.${exp.verdict}`, { defaultValue: exp.verdict.replace('_', ' ') })}</Chip>
          {exp.eligible_for_approval && <Chip tone="info" title={t('lab.experiments.eligibleTitle')}>{t('lab.experiments.eligible')}</Chip>}
          {exp.promotion_evidence === 'independently_supported' && <Chip tone="success" title={t('lab.experiments.independentTitle')}>{t('lab.experiments.independent')}</Chip>}
          {exp.promotion_evidence === 'exploratory_only' && <Chip tone="warning" title={t('lab.experiments.exploratoryTitle')}>{t('lab.experiments.exploratory')}</Chip>}
          {exp.next_action && NEXT_ACTION_TONE[exp.next_action]
            ? <Chip tone={NEXT_ACTION_TONE[exp.next_action]} title={t(`lab.nextAction.${exp.next_action}.title`)}>
                {t(`lab.nextAction.${exp.next_action}.label`)}</Chip>
            : exp.candidate_status === 'approved' && <Chip tone="accent">{t('lab.experiments.approved')}</Chip>}
        </div>
      </div>
      {exp.explanation && <div style={{ fontSize: '0.85rem', marginTop: '0.4rem' }}>{exp.explanation}</div>}
      <div style={{ fontSize: '0.82rem', marginTop: '0.4rem' }}>
        {t('lab.experiments.deltas', {
          fixed: exp.fixed ?? '–', broken: exp.broken ?? '–', precision: signed(d.precision, true), recall: signed(d.recall, true),
          unsafe: signed(d.unsafe_rejection_rate, true), fp: signed(d.fp), fn: signed(d.fn),
        })}
      </div>
      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginTop: '0.4rem' }}>
        {Object.entries(exp.guardrails).map(([name, ok]) => (
          <Chip key={name} tone={ok ? 'success' : 'error'}>{ok ? '✓' : '✗'} {name.replaceAll('_', ' ')}</Chip>
        ))}
      </div>
      {exp.limitations.length > 0 && (
        <ul style={{ margin: '0.4rem 0 0', paddingLeft: '1.1rem', fontSize: '0.78rem', color: 'var(--color-muted)' }}>
          {exp.limitations.map(l => <li key={l}>{l}</li>)}
        </ul>
      )}
    </Card>
  )
}

function MemoryList({ items }: { items: LabMemoryItem[] }) {
  const { t } = useTranslation()
  const recs = items.filter(i => i.kind === 'conclusion' && i.status === 'active')
  const refuted = items.filter(i => i.kind === 'hypothesis' && i.status === 'rejected')
  const evidence = items.filter(i => i.kind === 'evidence' && i.status === 'active')
  // `title` is a translation key with a {count} parameter.
  const block = (title: string, list: LabMemoryItem[]) => list.length === 0 ? null : (
    <div style={{ marginBottom: '0.7rem' }}>
      <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>{t(title, { count: list.length })}</div>
      <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.85rem' }}>{list.slice(0, 10).map(i => <li key={i.item_id}>{i.statement}</li>)}</ul>
    </div>
  )
  if (recs.length + refuted.length + evidence.length === 0) return <EmptyState message={t('lab.memory.empty')} />
  return <>{block('lab.memory.recommendations', recs)}{block('lab.memory.refuted', refuted)}{block('lab.memory.evidence', evidence)}</>
}

// Presentational: the latest pointer and what the pipeline actually runs (an approved bundle or config/).
export function ActiveStrategyView({ data }: { data: ActiveStrategy }) {
  const { t } = useTranslation()
  return (
    <div style={{ fontSize: '0.85rem' }}>
      {data.current
        ? t('lab.active.pointer', {
            pointer: data.current.pointer_id, bundle: String(data.current.bundle_hash).slice(0, 12), action: data.current.action,
            by: data.current.approved_by ? t('lab.active.by', { name: data.current.approved_by }) : '',
          })
        : <Trans i18nKey="lab.active.none" components={{ code: <code /> }} />}
      <div style={{ marginTop: '0.3rem' }}>
        <Chip tone={data.pipeline.status === 'active' ? 'success'
          : data.pipeline.status === 'invalid' || data.pipeline.status === 'stale' ? 'error' : 'info'}>
          {t(data.pipeline.status === 'active' ? 'lab.active.pipelineBundle' : 'lab.active.pipelineConfig', { status: data.pipeline.status })}
        </Chip>
        {data.pipeline.status === 'active' && data.pipeline.approval_basis === 'exploratory_only' && (
          <Chip tone="warning" title={t('lab.active.exploratoryTitle')}>
            {t('lab.active.exploratory')}</Chip>)}
        {data.pipeline.status === 'active' && data.pipeline.approval_basis === 'unrecorded' && (
          <Chip tone="warning" title={t('lab.active.unrecordedTitle')}>{t('lab.active.unrecorded')}</Chip>)}
      </div>
      <div style={{ color: 'var(--color-muted)', marginTop: '0.3rem' }}>{data.note}</div>
    </div>
  )
}

export default function Lab() {
  const { t } = useTranslation()
  const runs = useQuery({ queryKey: ['lab-runs'], queryFn: getEvalRuns })
  const exps = useQuery({ queryKey: ['lab-experiments'], queryFn: getLabExperiments })
  const memory = useQuery({ queryKey: ['lab-memory'], queryFn: getLabMemory })
  const active = useQuery({ queryKey: ['lab-active'], queryFn: getActiveStrategy })
  const check = useQuery({ queryKey: ['lab-self-check'], queryFn: getSelfCheck })
  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>{t('lab.title')}</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        <Trans i18nKey="lab.intro" components={{ cmd: <code>python -m src.lab sandbox run</code> }} />
      </p>
      <Card title={t('lab.selfCheck.title')} subtitle={t('lab.selfCheck.subtitle')}
        style={{ marginBottom: 'var(--space-4)' }}>
        {check.isLoading && <LoadingState />}
        {check.isError && <EmptyState message={t('lab.selfCheck.unavailable')} />}
        {check.data && <SelfCheckView data={check.data} />}
      </Card>
      <Card title={t('lab.active.title')} style={{ marginBottom: 'var(--space-4)' }}>
        {active.isLoading && <LoadingState />}
        {active.isError && <EmptyState message={t('lab.active.unavailable')} />}
        {active.data && <ActiveStrategyView data={active.data} />}
      </Card>
      <Card title={t('lab.runs.title')} subtitle={t('lab.runs.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
        {runs.isLoading && <LoadingState />}
        {runs.isError && <EmptyState message={t('lab.runs.unavailable')} />}
        {runs.data && runs.data.runs.length === 0 && <EmptyState message={t('lab.runs.empty')} />}
        {runs.data && runs.data.runs.length > 0 && (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', fontSize: '0.82rem', borderCollapse: 'collapse' }}>
              <thead><tr style={{ textAlign: 'left', color: 'var(--color-muted)' }}>
                {(['when', 'dataset', 'strategy', 'labelled', 'confusion', 'precision', 'recall', 'unsafe'] as const).map(c => <th key={c}>{t(`lab.runs.columns.${c}`)}</th>)}
              </tr></thead>
              <tbody>{runs.data.runs.map(r => <RunRow key={r.file} run={r} />)}</tbody>
            </table>
            {runs.data.unreadable > 0 && <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)' }}>{t('lab.runs.unreadable', { count: runs.data.unreadable })}</div>}
          </div>
        )}
      </Card>
      <Card title={t('lab.experiments.title')} subtitle={t('lab.experiments.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
        {exps.isLoading && <LoadingState />}
        {exps.isError && <EmptyState message={t('lab.experiments.unavailable')} />}
        {exps.data && exps.data.experiments.length === 0 && <EmptyState message={t('lab.experiments.empty')} />}
        {exps.data?.experiments.map(e => <ExperimentCard key={e.experiment_id} exp={e} />)}
      </Card>
      <Card title={t('lab.memory.title')}>
        {memory.isLoading && <LoadingState />}
        {memory.isError && <EmptyState message={t('lab.memory.unavailable')} />}
        {memory.data && <MemoryList items={memory.data.items} />}
      </Card>
    </div>
  )
}
