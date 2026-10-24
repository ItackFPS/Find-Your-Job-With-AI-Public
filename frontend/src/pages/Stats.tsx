import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getJobStats, type SkillFacet, type ScoreTierFacet, type RejectionFacet, type TitleCategoryFacet } from '../api/client'
import { Card, Ring, EmptyState, LoadingState } from '../components'
import { rejectionReasonLabel, type ScoreTier } from '../utils'

// Phase 4 (2026-09-10): fixed-reference calibration — see
// src/pipeline/scoring.py's _TIER_THRESHOLDS for the calibration
// methodology; mirrored here (and in ../utils/scoreToTier.ts) for display.
// Labels (with their score ranges) are translated under stats.tierRanges.<tier>.

const TIER_ORDER: ScoreTier[] = ['exceptional', 'excellent', 'strong', 'relevant', 'low_priority']

function RankedBarList({ items, color, total }: { items: { name: string; count: number }[]; color: string; total: number }) {
  const { t, i18n } = useTranslation()
  if (items.length === 0) return <EmptyState message={t('stats.notEnoughData')} />
  const max = Math.max(...items.map(i => i.count), 1)
  return (
    <div>
      {items.map((item, i) => (
        <div key={item.name} style={{ marginBottom: '0.55rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--font-size-sm)', marginBottom: '0.2rem' }}>
            <span>{i + 1}. {item.name}</span>
            <span style={{ color: 'var(--color-muted)' }}>
              {total > 0
                ? t('stats.countPercent', { count: item.count, percent: Math.round((item.count / total) * 100) })
                : item.count.toLocaleString(i18n.language)}
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
  const { t } = useTranslation()
  return (
    <Card>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-4)' }}>
        <Ring value={facet ? Math.min(100, facet.count) : 0} color={color} size={60}>
          {facet ? facet.count : '–'}
        </Ring>
        <div>
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', letterSpacing: '0.05em', marginBottom: '0.2rem' }}>{subtitle}</div>
          <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: 'var(--font-size-lg)' }}>{facet ? facet.name : t('stats.noDataYet')}</div>
          {facet && <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('stats.mentions', { count: facet.count })}</div>}
        </div>
      </div>
      <div style={{ marginTop: 'var(--space-2)', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>{title}</div>
    </Card>
  )
}

function ScoreTierCard({ tiers }: { tiers: ScoreTierFacet[] }) {
  const { t } = useTranslation()
  const total = tiers.reduce((sum, f) => sum + f.count, 0)
  const byTier = Object.fromEntries(tiers.map(f => [f.tier, f.count]))
  return (
    <Card title={t('stats.scoreDistribution')} subtitle={t('stats.latestRun')}>
      {total === 0 ? <EmptyState message={t('stats.noScoredRun')} /> : (
        <RankedBarList
          items={TIER_ORDER.filter(tier => byTier[tier]).map(tier => ({ name: t(`stats.tierRanges.${tier}`), count: byTier[tier] }))}
          color="var(--color-accent)"
          total={total}
        />
      )}
    </Card>
  )
}

function RejectionsCard({ rejections }: { rejections: RejectionFacet[] }) {
  const { t } = useTranslation()
  const total = rejections.reduce((sum, r) => sum + r.count, 0)
  return (
    <Card title={t('stats.rejections')} subtitle={t('stats.latestRun')}>
      {rejections.length === 0 ? <EmptyState message={t('stats.noRejections')} /> : (
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
  const { t } = useTranslation()
  const total = categories.reduce((sum, c) => sum + c.count, 0)
  return (
    <Card title={t('stats.titleCategories')} subtitle={t('stats.titleCategoriesSubtitle')}>
      {categories.length === 0 ? <EmptyState message={t('stats.noDataYetDot')} /> : (
        <RankedBarList items={categories} color="var(--color-accent)" total={total} />
      )}
    </Card>
  )
}

export default function Stats() {
  const { t } = useTranslation()
  const { data, isLoading, error } = useQuery({
    queryKey: ['job-stats'],
    queryFn: getJobStats,
    staleTime: 30_000,
  })

  if (isLoading) return <LoadingState />
  if (error) return <div style={{ color: 'var(--color-error)', padding: '2rem' }}>{t('stats.apiError')}</div>

  const stats = data!
  const topSkill = stats.skills[0]
  const topTool = stats.tools[0]

  return (
    <div>
      <div style={{ marginBottom: 'var(--space-6)' }}>
        <h1 style={{ margin: 0, fontSize: 'var(--font-size-xl)', fontWeight: 'var(--font-weight-bold)' }}>{t('stats.title')}</h1>
        <div style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-sm)', marginTop: '0.2rem' }}>
          {t('stats.subtitle', { count: stats.total_jobs })}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <HeadlineCard title={t('stats.topSkill')} subtitle={t('stats.topSkillCaps')} facet={topSkill} color="var(--color-accent)" />
        <HeadlineCard title={t('stats.topTool')} subtitle={t('stats.topToolCaps')} facet={topTool} color="var(--color-info)" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
        <Card title={t('stats.topSkills')} subtitle={t('stats.latestRun')}>
          <RankedBarList items={stats.skills.slice(0, 5)} color="var(--color-accent)" total={stats.total_jobs} />
        </Card>
        <Card title={t('stats.topTools')} subtitle={t('stats.latestRun')}>
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
