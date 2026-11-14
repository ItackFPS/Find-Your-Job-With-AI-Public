import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { forgetPreference, getLearnedPreferences, restorePreference } from '../../api/client'
import type { LearnedPreference } from '../../api/client'
import { Button } from '../Button'
import { useToast } from '../toastContext'
import { LEARNING_QUERY_KEY } from './queryKeys'

const QUERY_KEY = [...LEARNING_QUERY_KEY, 'preferences']

/**
 * Settings › Learning (ADR 0030): what the app learned from the user's actions, each item with a
 * way to forget it, and what that changed in the latest search.
 */
export function LearnedPreferences() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const { data } = useQuery({ queryKey: QUERY_KEY, queryFn: getLearnedPreferences })
  const onDone = () => { void queryClient.invalidateQueries({ queryKey: QUERY_KEY }) }
  const onError = () => toast.show({ message: t('settings.learning.failed'), tone: 'danger' })
  const forget = useMutation({ mutationFn: forgetPreference, onSuccess: onDone, onError })
  const restore = useMutation({ mutationFn: restorePreference, onSuccess: onDone, onError })
  if (!data?.preferences) return null

  const label = (p: Pick<LearnedPreference, 'feature' | 'value'>) =>
    p.feature === 'terms' ? p.value : t(`learning.values.${p.feature}`, { value: p.value, defaultValue: `${p.feature}: ${p.value}` })
  const small = { margin: 0, fontSize: 'var(--font-size-sm)' }
  const row = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-2)' }
  const compared = data.comparison

  return (
    <section data-testid="learned-preferences" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
      <h3 style={{ margin: 0, fontSize: 'var(--font-size-md)' }}>{t('learning.learned.title')}</h3>
      <p style={small}>
        {data.paused ? t('learning.learned.paused')
          : data.enough_evidence ? t('learning.learned.applied', { count: data.strong_signals })
            : t('learning.learned.needMore', { count: data.strong_signals, needed: data.strong_needed })}
      </p>
      {data.preferences.length > 0 && (
        <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
          {data.preferences.map(p => (
            <li key={`${p.feature}:${p.value}`} style={row}>
              <span style={small}>
                {p.weight > 0 ? t('learning.learned.likes') : t('learning.learned.avoids')} {label(p)}
              </span>
              <Button variant="ghost" disabled={forget.isPending} onClick={() => forget.mutate(p)}
                aria-label={t('learning.learned.forgetItem', { item: label(p) })}>
                {t('learning.learned.forget')}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {data.forgotten.length > 0 && (
        <ul style={{ margin: 0, padding: 0, listStyle: 'none' }} aria-label={t('learning.learned.forgottenTitle')}>
          {data.forgotten.map(p => (
            <li key={`${p.feature}:${p.value}`} style={row}>
              <span style={{ ...small, color: 'var(--color-muted)' }}>{label(p)}</span>
              <Button variant="ghost" disabled={restore.isPending} onClick={() => restore.mutate(p)}
                aria-label={t('learning.learned.restoreItem', { item: label(p) })}>
                {t('learning.learned.restore')}
              </Button>
            </li>
          ))}
        </ul>
      )}
      {compared && compared.moved > 0 && (
        <div data-testid="learning-comparison" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
          <p style={small}>
            {t(compared.applied ? 'learning.learned.movedApplied' : 'learning.learned.movedPreview',
              { moved: compared.moved, ranked: compared.ranked, top: compared.entered_top_20 })}
          </p>
          <ul style={{ margin: 0, paddingLeft: 'var(--space-4)' }}>
            {compared.movers.map(m => (
              <li key={m.job_id} style={small}>
                {t('learning.learned.mover', { title: m.title ?? m.job_id, from: m.production_position, to: m.personal_position })}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
