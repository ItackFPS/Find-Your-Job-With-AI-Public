import { useQuery } from '@tanstack/react-query'
import {
  getActiveStrategy, getEvalRuns, getLabExperiments, getLabMemory, type ActiveStrategy,
  type EvalRunSummary, type LabExperiment, type LabMemoryItem,
} from '../api/client'
import { Card, Chip, EmptyState, LoadingState, type ChipTone } from '../components'

const pct = (v: number | null | undefined) => (v == null ? '–' : `${Math.round(v * 1000) / 10}%`)
const signed = (v: number | null | undefined, asPct = false) =>
  v == null ? '–' : `${v > 0 ? '+' : ''}${asPct ? pct(v) : v}`

const VERDICT_TONE: Record<string, ChipTone> = {
  improvement: 'success', regression: 'error', inconclusive: 'warning', insufficient_evidence: 'default',
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
          {exp.candidate_status === 'approved' && <Chip tone="accent">approved</Chip>}
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
  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>Lab</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        Benchmark runs and strategy experiments, read-only. Changes are tested here on frozen labelled datasets and only a
        human approval (command line) can promote one.
      </p>
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
