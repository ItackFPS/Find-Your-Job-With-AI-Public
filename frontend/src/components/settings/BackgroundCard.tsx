import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getBackgroundSettings, updateBackgroundSettings, type BackgroundSettings } from '../../api/client'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { Switch } from '../Switch'
import { useToast } from '../toastContext'
import { BACKGROUND_QUERY_KEY, SCHEDULER_QUERY_KEY } from './queryKeys'

/**
 * Background collection on/off and the local time each day's runs start (issue #80).
 * Standalone, so the first-run wizard (P3) can show the same card.
 */
export function BackgroundCard() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch } = useQuery({ queryKey: BACKGROUND_QUERY_KEY, queryFn: getBackgroundSettings })
  // The time being edited; null shows the saved one.
  const [draft, setDraft] = useState<string | null>(null)
  const time = draft ?? data?.daily_time ?? ''

  const save = useMutation({
    mutationFn: (changes: Partial<BackgroundSettings>) => updateBackgroundSettings(changes),
    onSuccess: saved => {
      queryClient.setQueryData(BACKGROUND_QUERY_KEY, saved)
      setDraft(null)
      void queryClient.invalidateQueries({ queryKey: SCHEDULER_QUERY_KEY })
      toast.show({ message: t('settings.background.saved'), tone: 'success' })
    },
    onError: () => toast.show({ message: t('settings.background.saveFailed'), tone: 'danger' }),
  })

  return (
    <Card title={t('settings.background.title')} subtitle={t('settings.background.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <Switch
            checked={data.enabled}
            disabled={save.isPending}
            onChange={enabled => save.mutate({ enabled })}
            label={t('settings.background.enabled')}
            description={t(data.enabled ? 'settings.background.enabledOn' : 'settings.background.enabledOff')}
          />
          <form
            onSubmit={e => { e.preventDefault(); save.mutate({ daily_time: time }) }}
            style={{ display: 'flex', alignItems: 'flex-end', gap: 'var(--space-2)', flexWrap: 'wrap' }}
          >
            <label style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)', fontSize: 'var(--font-size-sm)' }}>
              {t('settings.background.time')}
              <input
                type="time"
                value={time}
                required
                onChange={e => setDraft(e.target.value)}
                disabled={!data.enabled || save.isPending}
              />
            </label>
            <Button type="submit" variant="secondary" disabled={!data.enabled || save.isPending || time === data.daily_time}>
              {t('settings.background.saveTime')}
            </Button>
          </form>
          <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
            {t('settings.background.timeHelp')}
          </p>
        </div>
      )}
    </Card>
  )
}
