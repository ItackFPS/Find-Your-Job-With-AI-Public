import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getLearningState, getLearningStatus, resetLearning, resumeLearning, setLearningPaused } from '../../api/client'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { Switch } from '../Switch'
import { useToast } from '../toastContext'
import { LearnedPreferences } from './LearnedPreferences'
import { LEARNING_QUERY_KEY } from './queryKeys'

/**
 * Settings › Learning (ADR 0015): pause learning (reversible) and reset learning (a marker:
 * learning ignores earlier signals; your actions, hidden offers and application tracker are kept).
 * It also says where the personal ranking stands (ADR 0019): while observing, one neutral sentence,
 * no counter and no request; when paused, a notice with "Reprendre" and "Réinitialiser".
 */
// Literal keys keep the catalogue check able to see them; an unknown server key falls back to "observing".
function statusWording(t: (key: string) => string, key: string): string {
  switch (key) {
    case 'learning.status.experimental': return t('learning.status.experimental')
    case 'learning.status.active': return t('learning.status.active')
    case 'learning.status.paused': return t('learning.status.paused')
    default: return t('learning.status.observing')
  }
}

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
  const status = useQuery({ queryKey: [...LEARNING_QUERY_KEY, 'status'], queryFn: getLearningStatus })
  const resume = useMutation({
    mutationFn: resumeLearning,
    onSuccess: () => {
      void refresh()
      toast.show({ message: t('learning.status.resumed'), tone: 'success' })
    },
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
          {status.data && !status.data.paused_notice && (
            <p data-testid="learning-status" style={{ margin: 0, fontSize: 'var(--font-size-sm)' }}>{statusWording(t, status.data.wording_key)}</p>
          )}
          {status.data?.paused_notice && (
            <div role="status" data-testid="learning-paused" style={{
              border: '1px solid var(--color-border)', borderRadius: 'var(--radius-sm)', padding: 'var(--space-3)',
              display: 'flex', flexDirection: 'column', gap: 'var(--space-2)',
            }}>
              <p style={{ margin: 0 }}>{t('learning.status.paused')}</p>
              <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('learning.status.resumeHelp')}</p>
              <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                <Button disabled={resume.isPending} onClick={() => resume.mutate()}>{t('learning.status.resume')}</Button>
                <Button variant="secondary" onClick={() => setConfirming(true)}>{t('learning.status.resetFromNotice')}</Button>
              </div>
            </div>
          )}
          <LearnedPreferences />
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
