import { useState, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getSkillGraph, type SkillGap, type SkillGraph, type SkillNode } from '../api/client'
import { Card, Chip, EmptyState, LoadingState } from '../components'

const WINDOWS = [30, 90, 180, 365]

const pill = (active: boolean): CSSProperties => ({
  border: `1px solid ${active ? 'var(--color-accent)' : 'var(--color-border)'}`,
  borderRadius: 'var(--radius-pill)',
  padding: '0.25rem 0.75rem',
  fontSize: '0.8rem',
  background: active ? 'color-mix(in srgb, var(--color-accent) 13%, transparent)' : 'transparent',
  color: active ? 'var(--color-accent)' : 'var(--color-muted)',
  cursor: 'pointer',
})

function Bar({ share, color }: { share: number; color: string }) {
  return (
    <div style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius-sm)', height: '6px', overflow: 'hidden' }}>
      <div style={{ width: `${Math.round(Math.min(1, share) * 100)}%`, height: '100%', background: color }} />
    </div>
  )
}

// Presentational: one development opportunity with the evidence behind it.
export function GapCard({ gap }: { gap: SkillGap }) {
  return (
    <Card compact style={{ marginBottom: 'var(--space-2)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem' }}>
        <strong>{gap.skill}</strong>
        <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>{Math.round(gap.relevant_share * 100)}% of relevant jobs</span>
      </div>
      <div style={{ margin: '0.35rem 0' }}><Bar share={gap.relevant_share} color="var(--color-warning)" /></div>
      <div style={{ fontSize: '0.85rem' }}>{gap.why}</div>
      {gap.example_titles.length > 0 && (
        <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)', marginTop: '0.3rem' }}>
          e.g. {gap.example_titles.join(' · ')}
        </div>
      )}
    </Card>
  )
}

// Presentational: the skills the market asks for, owned ones marked; selecting one lists its neighbours.
export function SkillList({ graph }: { graph: SkillGraph }) {
  const [selected, setSelected] = useState<string | null>(null)
  const related = selected
    ? graph.edges.filter(e => e.source === selected || e.target === selected)
      .map(e => ({ skill: e.source === selected ? e.target : e.source, weight: e.weight }))
      .sort((a, b) => b.weight - a.weight)
    : []
  const row = (n: SkillNode) => (
    <li key={n.skill} style={{ marginBottom: '0.5rem' }}>
      <button type="button" aria-pressed={selected === n.skill} onClick={() => setSelected(selected === n.skill ? null : n.skill)}
        style={{ ...pill(selected === n.skill), width: '100%', textAlign: 'left', display: 'block', borderRadius: 'var(--radius-sm)' }}>
        <span style={{ display: 'flex', justifyContent: 'space-between', gap: '0.5rem' }}>
          <span>{n.skill} {n.owned && <Chip tone="success">yours</Chip>}</span>
          <span>{n.relevant_demand} relevant / {n.demand} total</span>
        </span>
        <span style={{ display: 'block', marginTop: '0.25rem' }}>
          <Bar share={n.relevant_share} color={n.owned ? 'var(--color-success)' : 'var(--color-info)'} />
        </span>
      </button>
    </li>
  )
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)', gap: 'var(--space-4)' }}>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>{graph.nodes.map(row)}</ul>
      <div>
        <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: '0.4rem' }}>
          {selected ? `OFTEN ASKED WITH ${selected.toUpperCase()}` : 'SELECT A SKILL TO SEE RELATED SKILLS'}
        </div>
        {selected && related.length === 0 && <div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>No frequent pairing yet.</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {related.map(r => (
            <li key={r.skill} style={{ fontSize: '0.85rem', marginBottom: '0.3rem' }}>
              {r.skill} {graph.owned.includes(r.skill) && <Chip tone="success">yours</Chip>} — {r.weight} job(s)
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

export default function Skills() {
  const [days, setDays] = useState(90)
  const { data, isLoading, isError } = useQuery({ queryKey: ['skill-graph', days], queryFn: () => getSkillGraph(days) })
  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>Skills</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        Which technologies the jobs you are a fit for ask for, how they go together, and what your profile is missing —
        counted from the postings the pipeline kept.
      </p>
      <div style={{ display: 'flex', gap: '0.4rem', margin: '0.75rem 0' }}>
        {WINDOWS.map(d => (
          <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)} style={pill(days === d)}>{d} days</button>
        ))}
        {data && <span style={{ alignSelf: 'center', fontSize: '0.8rem', color: 'var(--color-muted)' }}>
          {data.n_relevant} relevant of {data.n_jobs} tagged jobs</span>}
      </div>
      {isLoading && <LoadingState />}
      {isError && <EmptyState message="The skill graph is unavailable." />}
      {data && data.nodes.length === 0 && <EmptyState message="No tagged jobs in this window yet." />}
      {data && data.nodes.length > 0 && (
        <>
          <Card title="Development opportunities" subtitle={`Relevant = ${data.relevant_definition}`} style={{ marginBottom: 'var(--space-4)' }}>
            {data.gaps.length === 0
              ? <EmptyState message="No skill is asked for often enough among relevant jobs that your profile lacks." />
              : data.gaps.slice(0, 8).map(g => <GapCard key={g.skill} gap={g} />)}
          </Card>
          <Card title="What the market asks for" style={{ marginBottom: 'var(--space-4)' }}>
            <SkillList graph={data} />
          </Card>
          <Card title="Limits of this view">
            <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.85rem', color: 'var(--color-muted)' }}>
              {data.limitations.map(l => <li key={l}>{l}</li>)}
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}
