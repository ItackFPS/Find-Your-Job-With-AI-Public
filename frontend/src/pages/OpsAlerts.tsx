import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getOpsAlerts, resolveOpsAlert, type OpsAlert } from '../api/client'
import { Button, Card, ErrorState, LoadingState } from '../components'
import { formatDateTime } from '../utils'

// Quality › Santé: self-monitoring alerts (ADR 0025). Each alert says what changed, the evidence and
// what to do; nothing here changes a source or a rule. Alerts close themselves when the cause clears.

const KEY = ['ops-alerts']
const pct = (v: unknown) => (typeof v === 'number' ? `${Math.round(v * 100)} %` : '–')
const num = (v: unknown) => (typeof v === 'number' ? String(v) : '–')
const list = (v: unknown) => (Array.isArray(v) ? v.join(', ') : '–')

export function AlertText({ a }: { a: OpsAlert }) {
  const { t } = useTranslation()
  const e = a.evidence
  const source = typeof e.source === 'string' ? e.source : a.scope
  if (a.code === 'source_volume_drop') {
    return <>
      <strong>{t('quality.alerts.volume.title', { source })}</strong>
      <span>{t('quality.alerts.volume.evidence', { recent: list(e.recent), median: num(e.median), runs: num(e.runs) })}</span>
      <span style={{ color: 'var(--color-muted)' }}>{t('quality.alerts.volume.action')}</span>
    </>
  }
  if (a.code === 'field_completeness_drop') {
    const field = typeof e.field === 'string' ? t(`quality.alerts.fields.${e.field}`, { defaultValue: e.field }) : '–'
    return <>
      <strong>{t('quality.alerts.completeness.title', { source, field })}</strong>
      <span>{t('quality.alerts.completeness.evidence', { today: pct(e.today), median: pct(e.median), offers: num(e.offers) })}</span>
      <span style={{ color: 'var(--color-muted)' }}>{t('quality.alerts.completeness.action')}</span>
    </>
  }
  return <strong>{t('quality.alerts.unknown', { code: a.code, scope: a.scope })}</strong>
}

export default function OpsAlertsCard() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const q = useQuery({ queryKey: KEY, queryFn: getOpsAlerts })
  const resolve = useMutation({
    mutationFn: resolveOpsAlert,
    onSettled: () => void queryClient.invalidateQueries({ queryKey: KEY }),
  })
  return (
    <Card title={t('quality.alerts.title')} subtitle={t('quality.alerts.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
      {q.isLoading && <LoadingState />}
      {q.isError && <ErrorState message={t('quality.alerts.error')} onRetry={() => void q.refetch()} />}
      {q.data && q.data.length === 0 && (
        <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('quality.alerts.none')}</p>
      )}
      {q.data && q.data.length > 0 && (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {q.data.map(a => (
            <li key={a.alert_id} role="alert"
              style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)', fontSize: 'var(--font-size-sm)' }}>
              <AlertText a={a} />
              <span style={{ color: 'var(--color-muted)' }}>
                {t('quality.alerts.since', { date: formatDateTime(a.raised_at) })}
              </span>
              <div>
                <Button variant="ghost" disabled={resolve.isPending} onClick={() => resolve.mutate(a.alert_id)}>
                  {t('quality.alerts.resolve')}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
