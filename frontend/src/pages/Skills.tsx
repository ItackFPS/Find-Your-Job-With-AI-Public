import { useState, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
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
  const { t } = useTranslation()
  return (
    <Card compact style={{ marginBottom: 'var(--space-2)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem' }}>
        <strong>{gap.skill}</strong>
        <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)' }}>{t('skills.shareOfRelevant', { percent: Math.round(gap.relevant_share * 100) })}</span>
      </div>
      <div style={{ margin: '0.35rem 0' }}><Bar share={gap.relevant_share} color="var(--color-warning)" /></div>
      <div style={{ fontSize: '0.85rem' }}>{gap.why}</div>
      {gap.example_titles.length > 0 && (
        <div style={{ fontSize: '0.78rem', color: 'var(--color-muted)', marginTop: '0.3rem' }}>
          {t('skills.examples', { titles: gap.example_titles.join(' · ') })}
        </div>
      )}
    </Card>
  )
}

// Presentational: the skills the market asks for, owned ones marked; selecting one lists its neighbours.
export function SkillList({ graph }: { graph: SkillGraph }) {
  const { t } = useTranslation()
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
          <span>{n.skill} {n.owned && <Chip tone="success">{t('skills.yours')}</Chip>}</span>
          <span>{t('skills.counts', { relevant: n.relevant_demand, total: n.demand })}</span>
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
          {selected ? t('skills.oftenAskedWith', { skill: selected.toUpperCase() }) : t('skills.selectSkill')}
        </div>
        {selected && related.length === 0 && <div style={{ fontSize: '0.85rem', color: 'var(--color-muted)' }}>{t('skills.noPairing')}</div>}
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {related.map(r => (
            <li key={r.skill} style={{ fontSize: '0.85rem', marginBottom: '0.3rem' }}>
              {r.skill} {graph.owned.includes(r.skill) && <Chip tone="success">{t('skills.yours')}</Chip>}{t('skills.pairJobs', { count: r.weight })}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

export default function Skills() {
  const { t } = useTranslation()
  const [days, setDays] = useState(90)
  const { data, isLoading, isError } = useQuery({ queryKey: ['skill-graph', days], queryFn: () => getSkillGraph(days) })
  return (
    <div>
      <h1 style={{ marginBottom: '0.3rem' }}>{t('skills.title')}</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0, fontSize: '0.9rem' }}>
        {t('skills.intro')}
      </p>
      <div style={{ display: 'flex', gap: '0.4rem', margin: '0.75rem 0' }}>
        {WINDOWS.map(d => (
          <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)} style={pill(days === d)}>{t('skills.days', { count: d })}</button>
        ))}
        {data && <span style={{ alignSelf: 'center', fontSize: '0.8rem', color: 'var(--color-muted)' }}>
          {t('skills.relevantOf', { relevant: data.n_relevant, total: data.n_jobs })}</span>}
      </div>
      {isLoading && <LoadingState />}
      {isError && <EmptyState message={t('skills.unavailable')} />}
      {data && data.nodes.length === 0 && <EmptyState message={t('skills.empty')} />}
      {data && data.nodes.length > 0 && (
        <>
          <Card title={t('skills.opportunities')} subtitle={t('skills.relevantDefinition', { definition: data.relevant_definition })} style={{ marginBottom: 'var(--space-4)' }}>
            {data.gaps.length === 0
              ? <EmptyState message={t('skills.noGaps')} />
              : data.gaps.slice(0, 8).map(g => <GapCard key={g.skill} gap={g} />)}
          </Card>
          <Card title={t('skills.market')} style={{ marginBottom: 'var(--space-4)' }}>
            <SkillList graph={data} />
          </Card>
          <Card title={t('skills.limits')}>
            <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.85rem', color: 'var(--color-muted)' }}>
              {data.limitations.map(l => <li key={l}>{l}</li>)}
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}
