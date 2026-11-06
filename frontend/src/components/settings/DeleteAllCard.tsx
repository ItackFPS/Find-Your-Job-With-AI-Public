import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { deleteAllData, getDeleteAll, retryDeleteAll, type DeleteAllStatus } from '../../api/client'
import { clearLocalData } from '../../api/feedbackOutbox'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { useToast } from '../toastContext'
import { DELETE_ALL_QUERY_KEY } from './queryKeys'

const ongoing = (s?: DeleteAllStatus) => !!s && ((s.pending && s.state !== 'BLOCKED') || !!s.restarting)
const POLL_MS = 2000

/**
 * Settings › Delete all application data (ADR 0015): every file of the app (database, backups,
 * calibration, lab, personal profile, logs). When Windows keeps a file locked, the launcher restarts
 * the backend once and its startup finishes the deletion; the card keeps polling across that gap and
 * confirms the result. The browser's own data is cleared only once the server reports COMPLETE.
 */
export function DeleteAllCard() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  // set once a deletion started here, or the server said it restarts: an unreachable backend is
  // then the restart in progress, not a failure
  const [awaitingRestart, setAwaitingRestart] = useState(false)
  const track = (s: DeleteAllStatus) => {
    if (s.restarting) setAwaitingRestart(true)
    else if (s.state === 'COMPLETE' || s.state === 'BLOCKED' || !s.pending) setAwaitingRestart(false)
    return s
  }
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: DELETE_ALL_QUERY_KEY,
    queryFn: () => getDeleteAll().then(track),
    retry: false,
    refetchInterval: q => (ongoing(q.state.data) || (awaitingRestart && q.state.status === 'error') ? POLL_MS : false),
  })
  const done = data?.state === 'COMPLETE' && !data.pending
  const restarting = !done && (!!data?.restarting || (awaitingRestart && isError))
  useEffect(() => { if (done) clearLocalData() }, [done])
  const onDone = (s: DeleteAllStatus) => {
    setConfirming(false)
    queryClient.setQueryData(DELETE_ALL_QUERY_KEY, track(s))
  }
  const failed = () => {
    setConfirming(false)
    toast.show({ message: t('settings.deleteAll.failed'), tone: 'danger' })
  }
  const start = useMutation({ mutationFn: deleteAllData, onSuccess: onDone, onError: failed })
  const retry = useMutation({ mutationFn: retryDeleteAll, onSuccess: onDone, onError: failed })

  return (
    <Card title={t('settings.deleteAll.title')} subtitle={t('settings.deleteAll.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && !restarting && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {restarting && <p role="status" style={{ margin: 0 }}>{t('settings.deleteAll.restarting')}</p>}
      {data && !restarting && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {ongoing(data) && <p role="status" style={{ margin: 0 }}>{t('settings.deleteAll.running')}</p>}
          {data.pending && data.state === 'BLOCKED' && (
            <div role="alert" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              <span>{(data.auto_restarts ?? 0) > 0
                ? t('settings.deleteAll.blockedAfterRestart', { reason: data.last_error ?? '' })
                : t('settings.deleteAll.blocked', { reason: data.last_error ?? '' })}</span>
              <div>
                <Button variant="secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>
                  {t('settings.deleteAll.retry')}
                </Button>
              </div>
            </div>
          )}
          {done && (
            <p role="status" style={{ margin: 0 }}>
              {data.restart_required ? t('settings.deleteAll.complete') : t('settings.deleteAll.completeRestarted')}
            </p>
          )}
          {!data.pending && !done && (
            <>
              <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
                {t('settings.deleteAll.help')}
              </p>
              {!confirming ? (
                <div>
                  <Button variant="danger" onClick={() => setConfirming(true)}>{t('settings.deleteAll.action')}</Button>
                </div>
              ) : (
                <div role="alertdialog" aria-label={t('settings.deleteAll.action')}
                  style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                  <span>{t('settings.deleteAll.confirm')}</span>
                  <Button variant="danger" disabled={start.isPending} onClick={() => start.mutate()}>
                    {t('settings.deleteAll.confirmYes')}
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
