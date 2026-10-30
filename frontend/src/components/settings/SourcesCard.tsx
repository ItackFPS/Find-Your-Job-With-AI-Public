import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getSourcesOverview, resetSourceBreaker, setSourceChoice, type SourceOverview } from '../../api/client'
import { formatDateTime } from '../../utils/formatDate'
import { Button } from '../Button'
import { Card } from '../Card'
import { Chip } from '../Chip'
import { ErrorState } from '../ErrorState'
import { LoadingState } from '../LoadingState'
import { SourceStatus } from '../SourceStatus'
import { Switch } from '../Switch'
import { useToast } from '../toastContext'
import { SCHEDULER_QUERY_KEY, SOURCES_QUERY_KEY } from './queryKeys'

function SourceRow({ source, onChoice, onReset, busy }: {
  source: SourceOverview
  onChoice: (enabled: boolean) => void
  onReset: () => void
  busy: boolean
}) {
  const { t } = useTranslation()
  const name = t(`settings.sources.names.${source.name}`, { defaultValue: source.name })
  const last = source.last_run
  return (
    <li aria-label={name} style={{ borderTop: '1px solid var(--color-border)', paddingTop: 'var(--space-3)', display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 'var(--font-weight-semibold)' }}>{name}</span>
        {source.access_class && <Chip>{t(`settings.sources.access.${source.access_class}`)}</Chip>}
        <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {t(source.collected ? 'settings.sources.collected' : 'settings.sources.notCollected')}
        </span>
      </div>
      {source.choice && (
        <div>
          <Switch
            checked={source.choice.enabled}
            disabled={busy || !source.available}
            onChange={onChoice}
            label={t('settings.sources.choose', { name })}
            description={t(`settings.sources.risk.${source.name}`, { defaultValue: t('settings.sources.risk.default') })}
          />
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginTop: 'var(--space-1)' }}>
            {source.choice.chosen_at
              ? t(source.choice.enabled ? 'settings.sources.chosenOn' : 'settings.sources.declinedOn', { when: formatDateTime(source.choice.chosen_at) })
              : t('settings.sources.neverChosen')}
            {!source.available && ` · ${t('settings.sources.unavailable')}`}
          </div>
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--space-3)', flexWrap: 'wrap', fontSize: 'var(--font-size-sm)' }}>
        <span style={{ color: 'var(--color-muted)' }}>{t('settings.sources.lastSearch')}</span>
        {last ? (
          <>
            <SourceStatus status={last.status} reason={last.reason} />
            {last.collected != null && (
              <span>{t('settings.sources.counts', { collected: last.collected, accepted: last.accepted ?? 0 })}</span>
            )}
          </>
        ) : (
          <span style={{ color: 'var(--color-muted)' }}>{t('settings.sources.noResult')}</span>
        )}
      </div>
      {source.breaker?.paused && (
        <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', flexWrap: 'wrap', color: 'var(--color-warning)', fontSize: 'var(--font-size-sm)' }}>
          {source.breaker.paused_until
            ? t('settings.sources.pausedUntil', { when: formatDateTime(source.breaker.paused_until), count: source.breaker.consecutive_failures })
            : t('settings.sources.paused')}
          <Button variant="ghost" disabled={busy} onClick={onReset}>{t('settings.sources.retryNow')}</Button>
        </div>
      )}
    </li>
  )
}

/**
 * Every source: its kind, whether it is collected, its latest result, a paused source's
 * reset, and the choice for an unofficial one (consent recorded with its date, F6.5).
 * Standalone, so the first-run wizard (P3) can reuse it.
 */
export function SourcesCard() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, refetch } = useQuery({ queryKey: SOURCES_QUERY_KEY, queryFn: getSourcesOverview })
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: SOURCES_QUERY_KEY })
    void queryClient.invalidateQueries({ queryKey: SCHEDULER_QUERY_KEY })
  }
  const choose = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) => setSourceChoice(name, enabled),
    onSuccess: refresh,
    onError: () => toast.show({ message: t('settings.sources.choiceFailed'), tone: 'danger' }),
  })
  const reset = useMutation({
    mutationFn: (name: string) => resetSourceBreaker(name),
    onSuccess: () => { toast.show({ message: t('settings.sources.resetDone'), tone: 'success' }); refresh() },
    onError: () => toast.show({ message: t('settings.sources.resetFailed'), tone: 'danger' }),
  })

  const groups: [string, SourceOverview[]][] = data
    ? [
        ['official', data.sources.filter(s => s.access_class !== 'unofficial')],
        ['unofficial', data.sources.filter(s => s.access_class === 'unofficial')],
      ]
    : []

  return (
    <Card title={t('settings.sources.title')} subtitle={t('settings.sources.subtitle')}>
      {isLoading && <LoadingState />}
      {isError && <ErrorState message={t('settings.loadFailed')} onRetry={() => void refetch()} />}
      {groups.map(([group, rows]) => rows.length > 0 && (
        <section key={group} aria-label={t(`settings.sources.groups.${group}`)} style={{ marginBottom: 'var(--space-4)' }}>
          <h3 style={{ fontSize: 'var(--font-size-md)', margin: '0 0 var(--space-1)' }}>{t(`settings.sources.groups.${group}`)}</h3>
          <p style={{ margin: '0 0 var(--space-2)', fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
            {t(`settings.sources.groupHelp.${group}`)}
          </p>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            {rows.map(s => (
              <SourceRow
                key={s.name}
                source={s}
                busy={choose.isPending || reset.isPending}
                onChoice={enabled => choose.mutate({ name: s.name, enabled })}
                onReset={() => reset.mutate(s.name)}
              />
            ))}
          </ul>
        </section>
      ))}
    </Card>
  )
}
