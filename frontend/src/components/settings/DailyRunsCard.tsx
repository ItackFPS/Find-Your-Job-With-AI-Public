import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getSchedulerStatus, runScheduleNow, type ScheduleStatus } from '../../api/client'
import { formatDateTime } from '../../utils/formatDate'
import { Button } from '../Button'
import { Card } from '../Card'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { StatusBadge } from '../StatusBadge'
import { useToast } from '../toastContext'
import { SCHEDULER_QUERY_KEY } from './queryKeys'

// What a schedule does on its own, in plain words.
function cadence(s: ScheduleStatus, t: (key: string, opts?: Record<string, unknown>) => string): string {
  if (s.is_running) return t('settings.runs.runningNow')
  if (!s.daily) {
    if (s.startup_policy === 'when_opted_in') return t('settings.runs.notChosen')
    if (s.startup_policy === 'when_profile_saved') return t('settings.runs.needsProfile')
    return t('settings.runs.manualOnly')
  }
  if (s.due) return t('settings.runs.dueToday')
  return s.next_run_at ? t('settings.runs.next', { when: formatDateTime(s.next_run_at) }) : t('settings.runs.manualOnly')
}

function LastRun({ s }: { s: ScheduleStatus }) {
  const { t } = useTranslation()
  const last = s.last_execution
  if (!last) return <span style={{ color: 'var(--color-muted)' }}>{t('settings.runs.never')}</span>
  const when = formatDateTime(last.finished_at ?? last.started_at)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
      <span style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
        <StatusBadge status={last.status} />
        <span style={{ fontSize: 'var(--font-size-sm)' }}>{when}</span>
      </span>
      {(last.status === 'failed' || last.status === 'interrupted') && (
        <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }} title={last.error ?? undefined}>
          {t(last.status === 'failed' ? 'settings.runs.failedHelp' : 'settings.runs.interruptedHelp')}
        </span>
      )}
    </div>
  )
}

/**
 * Each daily run (the search, LinkedIn, HelloWork): last run, next run, Run now.
 * Standalone, so the first-run wizard (P3) can reuse it.
 */
export function DailyRunsCard() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: SCHEDULER_QUERY_KEY, queryFn: getSchedulerStatus, refetchInterval: 15000,
  })
  const run = useMutation({
    mutationFn: (id: string) => runScheduleNow(id),
    onSuccess: () => {
      toast.show({ message: t('settings.runs.started'), tone: 'success' })
      void queryClient.invalidateQueries({ queryKey: SCHEDULER_QUERY_KEY })
    },
    onError: (err: unknown) => {
      const status = (err as { response?: { status?: number } }).response?.status
      toast.show({ message: t(status === 409 ? 'settings.runs.busy' : 'settings.runs.startFailed'), tone: status === 409 ? 'warning' : 'danger' })
    },
  })

  return (
    <Card title={t('settings.runs.title')} subtitle={t('settings.runs.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {data?.paused && (
        <p role="status" style={{ marginTop: 0, color: 'var(--color-warning)' }}>{t('settings.runs.paused')}</p>
      )}
      {data && (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          {data.schedules.filter(s => s.enabled).sort((a, b) => Number(b.source_id === 'pipeline') - Number(a.source_id === 'pipeline')).map(s => (
            <li
              key={s.id}
              aria-label={t(`settings.runs.labels.${s.id}`, { defaultValue: s.label })}
              style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-3)', alignItems: 'center', borderTop: '1px solid var(--color-border)', paddingTop: 'var(--space-3)' }}
            >
              <div style={{ flex: '1 1 12rem', minWidth: 0 }}>
                <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>{t(`settings.runs.labels.${s.id}`, { defaultValue: s.label })}</div>
                <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{cadence(s, t)}</div>
              </div>
              <div style={{ flex: '1 1 12rem', minWidth: 0 }}><LastRun s={s} /></div>
              <Button variant="secondary" disabled={s.is_running || run.isPending} onClick={() => run.mutate(s.id)}>
                {t('settings.runs.runNow')}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
