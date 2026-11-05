import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { eraseActivity, getErasure, retryErasure, type ErasureStatus } from '../../api/client'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { useToast } from '../toastContext'
import { ERASURE_QUERY_KEY } from './queryKeys'

const muted = { margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }
const row = { display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }

/** Ongoing while the marker exists and the job is not BLOCKED: the status is polled until it ends. */
const ongoing = (s?: ErasureStatus) => !!s && s.pending && s.state !== 'BLOCKED'

/**
 * Settings › Erase my activity (ADR 0015): deletes your actions, what was learned from them, older
 * backups and (by default) calibration answers. "Erasure complete" appears only once the job
 * reached COMPLETE, i.e. after its checks passed.
 */
export function EraseCard() {
  const { t, i18n } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const [keepTracker, setKeepTracker] = useState(false)
  const [eraseCalibration, setEraseCalibration] = useState(true)
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ERASURE_QUERY_KEY,
    queryFn: getErasure,
    refetchInterval: q => (ongoing(q.state.data) ? 2000 : false),
  })
  const onDone = (s: ErasureStatus) => {
    setConfirming(false)
    queryClient.setQueryData(ERASURE_QUERY_KEY, s)
    void queryClient.invalidateQueries()
  }
  const failed = () => {
    setConfirming(false)
    toast.show({ message: t('settings.erase.failed'), tone: 'danger' })
  }
  const erase = useMutation({
    mutationFn: () => eraseActivity({ keep_tracker: keepTracker, erase_calibration: eraseCalibration }),
    onSuccess: onDone,
    onError: failed,
  })
  const retry = useMutation({ mutationFn: retryErasure, onSuccess: onDone, onError: failed })

  return (
    <Card title={t('settings.erase.title')} subtitle={t('settings.erase.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {ongoing(data) && (
            <p role="status" style={{ margin: 0 }}>{t('settings.erase.running', { step: data.state ?? '' })}</p>
          )}
          {data.pending && data.state === 'BLOCKED' && (
            <div role="alert" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              <span>{t('settings.erase.blocked', { reason: data.last_error ?? '' })}</span>
              <div>
                <Button variant="secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>
                  {t('settings.erase.retry')}
                </Button>
              </div>
            </div>
          )}
          {!data.pending && data.state === 'COMPLETE' && data.completed_at && (
            <p role="status" style={{ margin: 0 }}>
              {t('settings.erase.complete', { date: new Date(data.completed_at).toLocaleString(i18n.language) })}
            </p>
          )}
          {!data.pending && (
            <>
              <p style={muted}>{t('settings.erase.help')}</p>
              <label style={row}>
                <input type="checkbox" checked={keepTracker} onChange={e => setKeepTracker(e.target.checked)} />
                {t('settings.erase.keepTracker')}
              </label>
              <label style={row}>
                <input type="checkbox" checked={eraseCalibration} onChange={e => setEraseCalibration(e.target.checked)} />
                {t('settings.erase.eraseCalibration')}
              </label>
              {!confirming ? (
                <div>
                  <Button variant="secondary" onClick={() => setConfirming(true)}>{t('settings.erase.action')}</Button>
                </div>
              ) : (
                <div role="alertdialog" aria-label={t('settings.erase.action')} style={{ ...row, flexWrap: 'wrap' }}>
                  <span>{t('settings.erase.confirm')}</span>
                  <Button variant="danger" disabled={erase.isPending} onClick={() => erase.mutate()}>
                    {t('settings.erase.confirmYes')}
                  </Button>
                  <Button variant="ghost" onClick={() => setConfirming(false)}>{t('settings.data.cancel')}</Button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </Card>
  )
}
