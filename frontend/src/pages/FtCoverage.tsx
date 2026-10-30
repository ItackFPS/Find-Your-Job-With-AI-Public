import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getFtCoverage, type FtCoverage } from '../api/client'
import { Card, ErrorState, LoadingState } from '../components'

// Quality › France Travail web coverage (plan F0.3, completion plan P2.7): of the offers the France Travail
// API found over the last 7 days, how many the web collector found too. Counts and a status only.

const KNOWN = new Set(['measured', 'too_early', 'no_api_offers', 'no_web_offers'])
const pct = (v: number | null | undefined) => (v == null ? '–' : `${Math.round(v * 100)} %`)

export function CoverageSentence({ c }: { c: FtCoverage }) {
  const { t } = useTranslation()
  const status = KNOWN.has(c.status) ? c.status : 'unexpected'
  return (
    <div style={{ fontSize: 'var(--font-size-sm)', display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      {(status === 'measured' || status === 'too_early') && (
        <p style={{ margin: 0 }}>{t('quality.coverage.share', {
          found: c.found_by_web, total: c.api_offers, share: pct(c.share), lo: pct(c.ci95?.[0]), hi: pct(c.ci95?.[1]),
        })}</p>
      )}
      <p style={{ margin: 0, color: 'var(--color-muted)' }}>
        {t(`quality.coverage.status.${status}`, { days: c.days_with_both, window: c.window_days })}
      </p>
    </div>
  )
}

export default function FtCoverageCard() {
  const { t } = useTranslation()
  const q = useQuery({ queryKey: ['ft-coverage'], queryFn: getFtCoverage })
  return (
    <Card title={t('quality.coverage.title')} subtitle={t('quality.coverage.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
      {q.isLoading && <LoadingState />}
      {q.isError && <ErrorState message={t('quality.coverage.error')} onRetry={() => void q.refetch()} />}
      {q.data && <CoverageSentence c={q.data} />}
    </Card>
  )
}
