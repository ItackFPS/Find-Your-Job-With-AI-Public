import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getLearningCurve, getLearningStatus, type LearningCurve, type LearningStatus } from '../api/client'
import { Button, Card, ErrorState, LoadingState } from '../components'

// Quality › Personal ranking (ADR 0019, v5 L4.4): the offline learning curve and the live per-stratum
// counts, in the descriptive wording ADR 0019 requires ("would have ranked higher in X cases"),
// never "better" or a probability.

const muted = { margin: 0, color: 'var(--color-muted)' }

export function CurveReport({ c, status }: { c?: LearningCurve; status?: LearningStatus }) {
  const { t } = useTranslation()
  const strata = Object.entries(status?.strata ?? {}).filter(([, v]) => v.discordant > 0)
  return (
    <div style={{ fontSize: 'var(--font-size-sm)', display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      {c && (c.status === 'measured' && c.wording.n > 0
        ? <p data-testid="curve-summary" style={{ margin: 0 }}>{t('learning.curve.summary', { n: c.wording.n, x: c.wording.x })}</p>
        : <p style={muted}>{t('learning.curve.none')}</p>)}
      {strata.length > 0 && (
        <>
          <p style={{ margin: 0 }}>{t('learning.curve.strata')}</p>
          <ul style={{ margin: 0, paddingLeft: 'var(--space-4)' }}>
            {strata.map(([k, v]) => (
              <li key={k}>{t('learning.curve.stratum', {
                name: t(`learning.curve.names.${k}`, { defaultValue: k }), n: v.discordant, x: v.challenger_wins,
              })}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

export default function LearningCurveCard() {
  const { t } = useTranslation()
  const status = useQuery({ queryKey: ['learning-state', 'status'], queryFn: getLearningStatus })
  // The replay runs in a child process (up to a few minutes): only on request.
  const curve = useQuery({ queryKey: ['learning-curve'], queryFn: getLearningCurve, enabled: false, retry: false })
  return (
    <Card title={t('learning.curve.title')} subtitle={t('learning.curve.subtitle')} style={{ marginBottom: 'var(--space-4)' }}
      actions={<Button variant="secondary" disabled={curve.isFetching} onClick={() => void curve.refetch()}>{t('learning.curve.run')}</Button>}>
      {curve.isFetching && <LoadingState />}
      {curve.isError && <ErrorState message={t('learning.curve.error')} onRetry={() => void curve.refetch()} />}
      <CurveReport c={curve.data} status={status.data} />
    </Card>
  )
}
