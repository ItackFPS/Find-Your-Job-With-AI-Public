import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getDataSettings, startDataCleanup } from '../../api/client'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { useToast } from '../toastContext'
import { DATA_QUERY_KEY } from './queryKeys'

const AGES = ['30', '90', '180'] as const

function size(bytes: number, locale: string): string {
  const gb = bytes / 2 ** 30
  const fmt = (n: number) => n.toLocaleString(locale, { maximumFractionDigits: 1 })
  return gb >= 1 ? `${fmt(gb)} GB` : `${fmt(bytes / 2 ** 20)} MB`
}

/**
 * Settings › Data (issue #86): how big the database is, and removing old expired offers
 * after a confirmation, then compacting the file. Offers the user acted on are kept.
 */
export function DataCard() {
  const { t, i18n } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [age, setAge] = useState<(typeof AGES)[number]>('90')
  const [confirming, setConfirming] = useState(false)
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: DATA_QUERY_KEY,
    queryFn: getDataSettings,
    refetchInterval: q => (q.state.data?.cleanup.state === 'running' ? 2000 : false),
  })
  const start = useMutation({
    mutationFn: () => startDataCleanup(Number(age)),
    onSuccess: () => { setConfirming(false); void queryClient.invalidateQueries({ queryKey: DATA_QUERY_KEY }) },
    onError: (err: unknown) => {
      setConfirming(false)
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      const known = ['search_running', 'scan_running', 'cleanup_running'].includes(detail ?? '')
      toast.show({ message: t(known ? `settings.data.busy.${detail}` : 'settings.data.failed'), tone: known ? 'warning' : 'danger' })
    },
  })

  const count = data?.removable_expired[age] ?? 0
  const cleanup = data?.cleanup
  const running = cleanup?.state === 'running'

  return (
    <Card title={t('settings.data.title')} subtitle={t('settings.data.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <p style={{ margin: 0 }}>
            {t('settings.data.summary', {
              size: size(data.database_bytes, i18n.language), active: data.offers.active, expired: data.offers.expired,
            })}
          </p>
          {data.backups.length > 0 && (
            <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
              {t('settings.data.backups', { count: data.backups.length, size: size(data.backups.reduce((n, b) => n + b.bytes, 0), i18n.language) })}
            </p>
          )}
          <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap', fontSize: 'var(--font-size-sm)' }}>
            {t('settings.data.olderThan')}
            <select value={age} onChange={e => { setAge(e.target.value as (typeof AGES)[number]); setConfirming(false) }} disabled={running}>
              {AGES.map(a => <option key={a} value={a}>{t('settings.data.days', { count: Number(a) })}</option>)}
            </select>
          </label>
          <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('settings.data.keeps')}</p>
          {!confirming ? (
            <div>
              <Button variant="secondary" disabled={running || count === 0} onClick={() => setConfirming(true)}>
                {t('settings.data.remove', { count })}
              </Button>
            </div>
          ) : (
            <div role="alertdialog" aria-label={t('settings.data.confirmTitle')} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
              <span>{t('settings.data.confirm', { count })}</span>
              <Button variant="danger" disabled={start.isPending} onClick={() => start.mutate()}>{t('settings.data.confirmYes')}</Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>{t('settings.data.cancel')}</Button>
            </div>
          )}
          {running && <p role="status" style={{ margin: 0 }}>{t('settings.data.running')}</p>}
          {cleanup?.state === 'done' && (
            <p role="status" style={{ margin: 0, color: 'var(--color-success)' }}>
              {t('settings.data.done', {
                count: cleanup.removed ?? 0,
                before: size(cleanup.bytes_before ?? 0, i18n.language),
                after: size(cleanup.bytes_after ?? 0, i18n.language),
              })}
              {cleanup.backup && <> {t('settings.data.doneBackup', { name: cleanup.backup })}</>}
              {cleanup.compacted === false && <> {t('settings.data.notCompacted')}</>}
            </p>
          )}
          {cleanup?.state === 'failed' && (
            <p role="alert" style={{ margin: 0, color: 'var(--color-error)' }} title={cleanup.error ?? undefined}>
              {t(cleanup.error === 'backup_failed' ? 'settings.data.failedBackup' : 'settings.data.failedRun')}
            </p>
          )}
        </div>
      )}
    </Card>
  )
}
