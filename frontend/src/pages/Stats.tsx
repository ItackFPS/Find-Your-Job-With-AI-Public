import { useQuery } from '@tanstack/react-query'
import { getJobStats, type SkillFacet, type ScoreTierFacet, type RejectionFacet, type TitleCategoryFacet } from '../api/client'
import { Card, Ring, EmptyState, LoadingState } from '../components'
import { rejectionReasonLabel, type ScoreTier } from '../utils'

// Phase 4 (2026-09-10): fixed-reference calibration — see
// src/pipeline/scoring.py's _TIER_THRESHOLDS for the calibration
// methodology; mirrored here (and in ../utils/scoreToTier.ts) for display.
const TIER_META: Record<ScoreTier, { label: string; color: string }> = {
  exceptional: { label: 'Exceptional (≥80)', color: 'var(--color-accent)' },
  excellent: { label: 'Excellent (72–79)', color: 'var(--color-info)' },
  strong: { label: 'Strong (65–71)', color: '#5b9ee8' },
  relevant: { label: 'Relevant (60–64)', color: 'var(--color-warning)' },
  low_priority: { label: 'Low priority (<60)', color: 'var(--color-muted)' },
}

const TIER_ORDER: ScoreTier[] = ['exceptional', 'excellent', 'strong', 'relevant', 'low_priority']

function RankedBarList({ items, color, total }: { items: { name: string; count: number }[]; color: string; total: number }) {
  if (items.length === 0) return <EmptyState message="Not enough data yet." />
  const max = Math.max(...items.map(i => i.count), 1)
  return (
    <div>
      {items.map((item, i) => (
        <div key={item.name} style={{ marginBottom: '0.55rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--font-size-sm)', marginBottom: '0.2rem' }}>
            <span>{i + 1}. {item.name}</span>
            <span style={{ color: 'var(--color-muted)' }}>
              {item.count.toLocaleString()}{total > 0 ? ` (${Math.round((item.count / total) * 100)}%)` : ''}
            </span>
          </div>
          <div style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius-sm)', height: '6px', overflow: 'hidden' }}>
            <div style={{ width: `${Math.round((item.count / max) * 100)}%`, height: '100%', background: color, borderRadius: 'var(--radius-sm)' }} />
          </div>
        </div>
      ))}
    </div>
  )
}

function HeadlineCard({ title, subtitle, facet, color }: { title: string; subtitle: string; facet: SkillFacet | undefined; color: string }) {
  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-4)' }}>
        <Ring value={facet ? Math.min(100, facet.count) : 0} color={color} size={60}>
          {facet ? facet.count : '–'}
        </Ring>
        <div>
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', letterSpacing: '0.05em', marginBottom: '0.2rem' }}>{subtitle}</div>
          <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: 'var(--font-size-lg)' }}>{facet ? facet.name : 'No data yet'}</div>
          {facet && <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{facet.count.toLocaleString()} jobs mention it</div>}
        </div>
      </div>
      <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>{title}</div>
    </Card>
  )
}

function ScoreTierCard({ tiers }: { tiers: ScoreTierFacet[] }) {
  const total = tiers.reduce((sum, t) => sum + t.count, 0)
  const byTier = Object.fromEntries(tiers.map(t => [t.tier, t.count]))
  return (
    <Card title="Match score distribution" subtitle="Latest completed run">
      {total === 0 ? <EmptyState message="No scored run yet." /> : (
        <RankedBarList
          items={TIER_ORDER.filter(t => byTier[t]).map(t => ({ name: TIER_META[t].label, count: byTier[t] }))}
          color="var(--color-accent)"
          total={total}
        />
      )}
    </Card>
  )
}

function RejectionsCard({ rejections }: { rejections: RejectionFacet[] }) {
  const total = rejections.reduce((sum, r) => sum + r.count, 0)
  return (
    <Card title="Why jobs get rejected" subtitle="Latest completed run">
      {rejections.length === 0 ? <EmptyState message="No rejection data yet." /> : (
        <RankedBarList
          items={rejections.map(r => ({ name: rejectionReasonLabel(r.detail), count: r.count }))}
          color="var(--color-error)"
          total={total}
        />
      )}
    </Card>
  )
}

function TitleCategoriesCard({ categories }: { categories: TitleCategoryFacet[] }) {
  const total = categories.reduce((sum, c) => sum + c.count, 0)
  return (
    <Card title="Job title categories" subtitle="Real titles from the latest run, grouped by the pipeline's own configured search queries">
      {categories.length === 0 ? <EmptyState message="No data yet." /> : (
        <RankedBarList items={categories} color="var(--color-accent)" total={total} />
      )}
    </Card>
  )
}

export default function Stats() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['job-stats'],
    queryFn: getJobStats,
    staleTime: 30_000,
  })

  if (isLoading) return <LoadingState />
  if (error) return <div style={{ color: 'var(--color-error)', padding: '2rem' }}>Could not reach API. Is the backend running?</div>

  const stats = data!
  const topSkill = stats.skills[0]
  const topTool = stats.tools[0]

  return (
    <div>
      <div style={{ marginBottom: 'var(--space-6)' }}>
        <h1 style={{ margin: 0, fontSize: 'var(--font-size-xl)', fontWeight: 'var(--font-weight-bold)' }}>Stats</h1>
        <div style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-sm)', marginTop: '0.2rem' }}>
          What the market is asking for, across {stats.total_jobs.toLocaleString()} collected jobs
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <HeadlineCard title="Most requested skill" subtitle="MOST REQUESTED SKILL" facet={topSkill} color="var(--color-accent)" />
        <HeadlineCard title="Most requested tool" subtitle="MOST REQUESTED TOOL" facet={topTool} color="var(--color-info)" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <Card title="Top skills in demand" subtitle="Latest completed run">
          <RankedBarList items={stats.skills.slice(0, 5)} color="var(--color-accent)" total={stats.total_jobs} />
        </Card>
        <Card title="Top tools & platforms" subtitle="Latest completed run">
          <RankedBarList items={stats.tools.slice(0, 5)} color="var(--color-info)" total={stats.total_jobs} />
        </Card>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <ScoreTierCard tiers={stats.score_tiers} />
        <RejectionsCard rejections={stats.rejections} />
      </div>

      <TitleCategoriesCard categories={stats.title_categories} />
    </div>
  )
}
