import { useQuery } from '@tanstack/react-query'
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

const NEXT_ACTION: Record<string, { label: string; tone: ChipTone; title: string }> = {
  submit_for_human_approval: { label: 'next: your review', tone: 'info', title: 'Worth a human decision: python -m src.lab approve (typed confirmation)' },
  reject: { label: 'next: discard', tone: 'error', title: 'The candidate did worse or broke a guardrail' },
  investigate: { label: 'next: investigate', tone: 'warning', title: 'Not enough evidence either way: more labelled jobs or another hypothesis' },
  approved: { label: 'approved by a human', tone: 'accent', title: 'Promoted through the human approval command' },
}

const STATUS: Record<SelfCheckStatus, { label: string; tone: ChipTone; text: string }> = {
  pass: { label: 'pass', tone: 'success', text: 'Same decisions as the recorded baseline, and no suitable job rejected.' },
  changed: { label: 'changed', tone: 'warning', text: 'Some decisions differ from the recorded baseline without breaking a labelled job: review them before refreshing the baseline.' },
  regression: { label: 'regression', tone: 'error', text: 'A job the baseline decided correctly is now decided wrongly.' },
  baseline_mismatch: { label: 'baseline mismatch', tone: 'error', text: 'The baseline was recorded on other data: nothing could be compared.' },
  safety_violation: { label: 'safety violation', tone: 'error', text: 'A job labelled suitable was rejected, or a rejection has no recorded reason.' },
}

const ANSWER_LABEL: Record<string, string> = {
  interested: 'interested', not_interested: 'not interested', unsure: 'unsure', ranking_feedback: 'ranking feedback',
}

// Presentational: explicit feedback on the latest decisions. These are the user's answers, never verified
// labels: a disagreement with a decision is a lead to review, and nothing here changes the self-check status.
export function SelfCheckFeedbackView({ fb }: { fb: SelfCheckFeedback }) {
  if (fb.status === 'unavailable') {
    return <div style={{ color: 'var(--color-muted)' }}>Feedback: not measured ({fb.why}).</div>
  }
  const answers = Object.entries(fb.latest_answers)
  const c = fb.contradictions
  return (
    <div>
      <div>
        Jobs with an answer: {fb.jobs_with_explicit_feedback} of {fb.decided_jobs} decided ({pct(fb.coverage)})
        {answers.length > 0 && <> · latest answers: {answers.map(([k, n]) => `${n} ${ANSWER_LABEL[k] ?? k}`).join(', ')}</>}
      </div>
      <div>
        Answer disagrees with the decision: {c.rejected_but_interested.count} rejected but interested ·{' '}
        {c.accepted_but_not_interested.count} accepted but not interested
      </div>
      <div>Decisions with recorded evidence: {pct(fb.explanation_coverage)}</div>
      <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)' }}>
        Your answers are signals, not verified labels: a disagreement is a job to review, not proof of an error.
      </div>
    </div>
  )
}

// Presentational: the latest self-check (current strategy vs the recorded baseline) in plain words.
export function SelfCheckView({ data }: { data: SelfCheckResponse }) {
  const r = data.latest
  if (!r) {
    return <EmptyState message="No self-check recorded yet. Run it from the Start Menu (Find Your Job With AI self-check), or from a source checkout: python -m src.evaluation self-check" />
  }
  const st = STATUS[r.status]
  return (
    <div style={{ fontSize: '0.85rem' }}>
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <Chip tone={st.tone}>{st.label}</Chip>
        <span>{st.text}</span>
      </div>
      <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)', marginTop: '0.3rem' }}>
        {r.created_at.slice(0, 16).replace('T', ' ')} · computed from decisions and labels ({r.generated_by}), no AI
      </div>
      {r.datasets.map(e => {
        const q = e.eligibility
        const flips = e.baseline.flips
        return (
          <div key={e.dataset.id} style={{ marginTop: '0.7rem' }}>
            <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>
              {e.dataset.id}@{e.dataset.version}{' '}
              <span style={{ color: 'var(--color-muted)', fontWeight: 'normal' }}>
                ({e.dataset.kind}, {e.dataset.n_jobs} jobs, {e.dataset.accept_labels} labelled suitable)
              </span>{' '}
              <Chip tone={STATUS[e.status].tone}>{STATUS[e.status].label}</Chip>
            </div>
            <div>
              Suitable jobs rejected:{' '}
              <strong style={{ color: e.safety.unsafe_rejections.length ? 'var(--color-error)' : undefined }}>
                {e.safety.unsafe_rejections.length}
              </strong>
              {' '}· unsuitable jobs let through: {q.fp ?? '–'} · precision {pct(q.precision)} · recall {pct(q.recall)}
              {' '}· rejections with a stated reason: {pct(e.explanations.coverage)}
            </div>
            {flips && flips.fixed.length + flips.broken.length + flips.unjudged.length > 0 && (
              <div>
                Since the baseline: {flips.fixed.length} fixed · {flips.broken.length} broken ·{' '}
                {flips.unjudged.length} unlabelled or borderline
              </div>
            )}
            {e.baseline.status === 'missing' && <div>No baseline is recorded for this dataset.</div>}
            <div style={{ color: 'var(--color-muted)' }}>Ranking quality: not measured — {e.ranking.why}.</div>
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
            YOUR FEEDBACK (SIGNALS, NOT LABELS)
          </div>
          <SelfCheckFeedbackView fb={r.feedback} />
        </div>
      )}
      {r.recommendations.length > 0 && (
        <div style={{ marginTop: '0.7rem' }}>
          <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>
            WHAT TO DO NEXT
          </div>
          <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
            {r.recommendations.map(x => (
              <li key={x.text}>
                <Chip tone={x.kind === 'action' ? 'warning' : 'default'}
                  title={x.kind === 'action' ? 'Follows from a measured result' : 'A suggestion to test in the lab, not a result'}>
                  {x.kind}
                </Chip>{' '}
                {x.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data.history.length > 1 && (
        <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)', marginTop: '0.5rem' }}>
          Earlier: {data.history.slice(1, 8).map(h => `${h.created_at.slice(0, 10)} ${h.status ?? '?'}`).join(' · ')}
        </div>
      )}
    </div>
  )
}

// Presentational: one evaluation run as a table row.
export function RunRow({ run }: { run: EvalRunSummary }) {
  const o = run.overall
  return (
    <tr>
      <td>{run.created_at.slice(0, 16).replace('T', ' ')}</td>
      <td>{run.dataset.id}@{run.dataset.version} <span style={{ color: 'var(--color-muted)' }}>({run.dataset.kind})</span></td>
      <td title={run.strategy.fingerprint}>{run.strategy.name} · {run.strategy.fingerprint.slice(0, 8)}</td>
      <td>{run.n_labelled ?? '–'}</td>
      <td>tp {o.tp ?? '–'} · fp {o.fp ?? '–'} · fn {o.fn ?? '–'} · tn {o.tn ?? '–'}</td>
      <td>{pct(o.precision)}</td>
      <td>{pct(o.recall)}</td>
      <td style={{ color: (o.unsafe_rejection_rate ?? 0) > 0 ? 'var(--color-error)' : undefined }}>{pct(o.unsafe_rejection_rate)}</td>
    </tr>
  )
}

// Presentational: one lab experiment with its verdict, guardrails, metric deltas and limits.
export function ExperimentCard({ exp }: { exp: LabExperiment }) {
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
          <Chip tone={VERDICT_TONE[exp.verdict] ?? 'default'}>{exp.verdict.replace('_', ' ')}</Chip>
          {exp.eligible_for_approval && <Chip tone="info" title="A human may approve it; nothing is promoted automatically">eligible for human approval</Chip>}
          {exp.promotion_evidence === 'independently_supported' && <Chip tone="success" title="The independent human labels alone support the improvement">independently supported</Chip>}
          {exp.promotion_evidence === 'exploratory_only' && <Chip tone="warning" title="Worth investigating only: the independent human labels alone do not support it">exploratory only — not independently validated</Chip>}
          {exp.next_action && NEXT_ACTION[exp.next_action]
            ? <Chip tone={NEXT_ACTION[exp.next_action].tone} title={NEXT_ACTION[exp.next_action].title}>
                {NEXT_ACTION[exp.next_action].label}</Chip>
            : exp.candidate_status === 'approved' && <Chip tone="accent">approved</Chip>}
        </div>
      </div>
      {exp.explanation && <div style={{ fontSize: '0.85rem', marginTop: '0.4rem' }}>{exp.explanation}</div>}
      <div style={{ fontSize: '0.82rem', marginTop: '0.4rem' }}>
        fixed {exp.fixed ?? '–'} · broken {exp.broken ?? '–'} · Δprecision {signed(d.precision, true)} · Δrecall {signed(d.recall, true)} ·
        Δunsafe rejection {signed(d.unsafe_rejection_rate, true)} · Δfp {signed(d.fp)} · Δfn {signed(d.fn)}
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
  const recs = items.filter(i => i.kind === 'conclusion' && i.status === 'active')
  const refuted = items.filter(i => i.kind === 'hypothesis' && i.status === 'rejected')
  const evidence = items.filter(i => i.kind === 'evidence' && i.status === 'active')
  const block = (title: string, list: LabMemoryItem[]) => list.length === 0 ? null : (
    <div style={{ marginBottom: '0.7rem' }}>
      <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.3rem' }}>{title} ({list.length})</div>
      <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.85rem' }}>{list.slice(0, 10).map(i => <li key={i.item_id}>{i.statement}</li>)}</ul>
    </div>
  )
  if (recs.length + refuted.length + evidence.length === 0) return <EmptyState message="The lab agent has not recorded anything yet." />
  return <>{block('RECOMMENDATIONS FOR YOUR REVIEW', recs)}{block('REFUTED HYPOTHESES', refuted)}{block('EVIDENCE', evidence)}</>
}

// Presentational: the latest pointer and what the pipeline actually runs (an approved bundle or config/).
export function ActiveStrategyView({ data }: { data: ActiveStrategy }) {
  return (
    <div style={{ fontSize: '0.85rem' }}>
      {data.current
        ? <>Pointer {data.current.pointer_id} → bundle {String(data.current.bundle_hash).slice(0, 12)} ({data.current.action}
          {data.current.approved_by ? ` by ${data.current.approved_by}` : ''})</>
        : <>No strategy has been promoted: the pipeline runs on <code>config/</code>.</>}
      <div style={{ marginTop: '0.3rem' }}>
        <Chip tone={data.pipeline.status === 'active' ? 'success'
          : data.pipeline.status === 'invalid' || data.pipeline.status === 'stale' ? 'error' : 'info'}>
          pipeline: {data.pipeline.status === 'active' ? 'approved bundle' : 'config/'} ({data.pipeline.status})
        </Chip>
        {data.pipeline.status === 'active' && data.pipeline.approval_basis === 'exploratory_only' && (
          <Chip tone="warning" title="Approved with an explicit acknowledgement that the independent human labels alone do not support it">
            approved on exploratory evidence — not independently validated</Chip>)}
        {data.pipeline.status === 'active' && data.pipeline.approval_basis === 'unrecorded' && (
          <Chip tone="warning" title="This approval predates recorded approval evidence">approval evidence not recorded</Chip>)}
      </div>
      <div style={{ color: 'var(--color-muted)', marginTop: '0.3rem' }}>{data.note}</div>
    </div>
  )
}

export default function Lab() {
  const runs = useQuery({ queryKey: ['lab-runs'], queryFn: getEvalRuns })
  const exps = useQuery({ queryKey: ['lab-experiments'], queryFn: getLabExperiments })
  const memory = useQuery({ queryKey: ['lab-memory'], queryFn: getLabMemory })
  const active = useQuery({ queryKey: ['lab-active'], queryFn: getActiveStrategy })
  const check = useQuery({ queryKey: ['lab-self-check'], queryFn: getSelfCheck })
  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>Lab</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        Benchmark runs and strategy experiments, read-only. Changes are tested here on frozen labelled datasets and only a
        human approval (command line) can promote one. What the lab agent writes is untrusted input. Nothing here starts it: run
        from its sandbox (<code>python -m src.lab sandbox run</code>, a Windows AppContainer) it cannot write your data
        or read your credentials; started any other way, only an in-process guard keeps it away from your data.
      </p>
      <Card title="Self-check" subtitle="the current strategy replayed on frozen labelled jobs, compared with the recorded baseline"
        style={{ marginBottom: 'var(--space-4)' }}>
        {check.isLoading && <LoadingState />}
        {check.isError && <EmptyState message="The self-check is unavailable." />}
        {check.data && <SelfCheckView data={check.data} />}
      </Card>
      <Card title="Active strategy" style={{ marginBottom: 'var(--space-4)' }}>
        {active.isLoading && <LoadingState />}
        {active.isError && <EmptyState message="The active strategy is unavailable." />}
        {active.data && <ActiveStrategyView data={active.data} />}
      </Card>
      <Card title="Evaluation runs" subtitle="python -m src.evaluation run … — metrics on labelled jobs" style={{ marginBottom: 'var(--space-4)' }}>
        {runs.isLoading && <LoadingState />}
        {runs.isError && <EmptyState message="Runs are unavailable." />}
        {runs.data && runs.data.runs.length === 0 && <EmptyState message="No evaluation run recorded yet." />}
        {runs.data && runs.data.runs.length > 0 && (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', fontSize: '0.82rem', borderCollapse: 'collapse' }}>
              <thead><tr style={{ textAlign: 'left', color: 'var(--color-muted)' }}>
                <th>When</th><th>Dataset</th><th>Strategy</th><th>Labelled</th><th>Confusion</th><th>Precision</th><th>Recall</th><th>Unsafe rejection</th>
              </tr></thead>
              <tbody>{runs.data.runs.map(r => <RunRow key={r.file} run={r} />)}</tbody>
            </table>
            {runs.data.unreadable > 0 && <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)' }}>{runs.data.unreadable} unreadable file(s) skipped</div>}
          </div>
        )}
      </Card>
      <Card title="Experiments" subtitle="baseline vs candidate on the same frozen dataset" style={{ marginBottom: 'var(--space-4)' }}>
        {exps.isLoading && <LoadingState />}
        {exps.isError && <EmptyState message="Experiments are unavailable." />}
        {exps.data && exps.data.experiments.length === 0 && <EmptyState message="No experiment yet." />}
        {exps.data?.experiments.map(e => <ExperimentCard key={e.experiment_id} exp={e} />)}
      </Card>
      <Card title="Agent memory">
        {memory.isLoading && <LoadingState />}
        {memory.isError && <EmptyState message="The agent memory is unavailable." />}
        {memory.data && <MemoryList items={memory.data.items} />}
      </Card>
    </div>
  )
}
