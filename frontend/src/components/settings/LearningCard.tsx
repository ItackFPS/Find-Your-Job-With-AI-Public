import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getLearningState, resetLearning, setLearningPaused } from '../../api/client'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { Switch } from '../Switch'
import { useToast } from '../toastContext'
import { LEARNING_QUERY_KEY } from './queryKeys'

/**
 * Settings › Learning (ADR 0015): pause learning (reversible) and reset learning (a marker:
 * learning ignores earlier signals; your actions, hidden offers and application tracker are kept).
 */
export function LearningCard() {
  const { t, i18n } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const { data, isLoading, isError, refetch } = useQuery({ queryKey: LEARNING_QUERY_KEY, queryFn: getLearningState })
  const refresh = () => queryClient.invalidateQueries({ queryKey: LEARNING_QUERY_KEY })
  const pause = useMutation({
    mutationFn: (paused: boolean) => setLearningPaused(paused),
    onSuccess: () => { void refresh() },
    onError: () => toast.show({ message: t('settings.learning.failed'), tone: 'danger' }),
  })
  const reset = useMutation({
    mutationFn: resetLearning,
    onSuccess: () => {
      setConfirming(false)
      void refresh()
      toast.show({ message: t('settings.learning.resetDone'), tone: 'success' })
    },
    onError: () => { setConfirming(false); toast.show({ message: t('settings.learning.failed'), tone: 'danger' }) },
  })

  return (
    <Card title={t('settings.learning.title')} subtitle={t('settings.learning.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <Switch
            checked={data.paused}
            disabled={pause.isPending}
            onChange={v => pause.mutate(v)}
            label={t('settings.learning.pause')}
            description={t('settings.learning.pauseHelp')}
          />
          {data.reset_at && (
            <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
              {t('settings.learning.lastReset', { date: new Date(data.reset_at).toLocaleString(i18n.language) })}
            </p>
          )}
          <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('settings.learning.resetHelp')}</p>
          {!confirming ? (
            <div>
              <Button variant="secondary" onClick={() => setConfirming(true)}>{t('settings.learning.reset')}</Button>
            </div>
          ) : (
            <div role="alertdialog" aria-label={t('settings.learning.reset')} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
              <span>{t('settings.learning.confirm')}</span>
              <Button variant="danger" disabled={reset.isPending} onClick={() => reset.mutate()}>{t('settings.learning.confirmYes')}</Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>{t('settings.data.cancel')}</Button>
            </div>
          )}
        </div>
      )}
    </Card>
  )
}
