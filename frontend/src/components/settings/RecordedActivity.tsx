import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { getRecordedActivity } from '../../api/client'

/**
 * What was recorded (Phase B B9-A): a read-only view of the behaviour data kept on this computer —
 * each kind of event with its count and how many learning may still count, the lists they came
 * from, and the offers shown per list and per source with their retention. Fetched only when opened.
 */
// Literal keys keep the catalogue check able to see them; an unknown type shows its stored name.
function typeLabel(t: (key: string) => string, type: string): string {
  switch (type) {
    case 'interested': return t('settings.learning.recorded.types.interested')
    case 'not_interested': return t('settings.learning.recorded.types.not_interested')
    case 'ranking_feedback': return t('settings.learning.recorded.types.ranking_feedback')
    case 'unsure': return t('settings.learning.recorded.types.unsure')
    case 'viewed': return t('settings.learning.recorded.types.viewed')
    case 'apply_link_opened': return t('settings.learning.recorded.types.apply_link_opened')
    case 'application_status_changed': return t('settings.learning.recorded.types.application_status_changed')
    case 'saved': return t('settings.learning.recorded.types.saved')
    case 'unsaved': return t('settings.learning.recorded.types.unsaved')
    case 'hidden': return t('settings.learning.recorded.types.hidden')
    case 'unhidden': return t('settings.learning.recorded.types.unhidden')
    default: return type
  }
}

const cell: React.CSSProperties = { padding: '0.2rem 0.5rem', textAlign: 'left', borderBottom: '1px solid var(--color-border)' }

function Counts({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts)
  if (entries.length === 0) return null
  return (
    <div>
      <div style={{ fontWeight: 600, margin: '0.5rem 0 0.2rem' }}>{title}</div>
      <div style={{ display: 'flex', gap: '0.4rem 1rem', flexWrap: 'wrap' }}>
        {entries.map(([k, n]) => <span key={k}><code>{k}</code> {n}</span>)}
      </div>
    </div>
  )
}

export function RecordedActivity({ open }: { open: boolean }) {
  const { t, i18n } = useTranslation()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['privacy-recorded'], queryFn: getRecordedActivity, enabled: open,
  })
  if (!open) return null
  if (isLoading) return <p style={{ margin: 0 }}>{t('settings.learning.recorded.loading')}</p>
  if (isError || !data) return <p style={{ margin: 0 }}>{t('settings.learning.recorded.unavailable')}</p>
  return (
    <div data-testid="recorded-activity" style={{ fontSize: 'var(--font-size-sm)', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
      <p style={{ margin: 0, color: 'var(--color-muted)' }}>{t('settings.learning.recorded.intro')}</p>
      {data.events.length === 0 ? (
        <p style={{ margin: 0 }}>{t('settings.learning.recorded.none')}</p>
      ) : (
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead>
            <tr>
              <th style={cell}>{t('settings.learning.recorded.colType')}</th>
              <th style={cell}>{t('settings.learning.recorded.colCount')}</th>
              <th style={cell}>{t('settings.learning.recorded.colCounted')}</th>
              <th style={cell}>{t('settings.learning.recorded.colLast')}</th>
            </tr>
          </thead>
          <tbody>
            {data.events.map(e => (
              <tr key={`${e.signal_class}:${e.type}`}>
                <td style={cell}>{typeLabel(t, e.type)}</td>
                <td style={cell}>{e.count}</td>
                <td style={cell}>{e.counted}</td>
                <td style={cell}>{e.last_at ? new Date(e.last_at).toLocaleString(i18n.language) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Counts title={t('settings.learning.recorded.eventSurfaces')} counts={data.event_surfaces} />
      <Counts title={t('settings.learning.recorded.exposuresBySurface', { count: data.exposures.total })} counts={data.exposures.by_surface} />
      <Counts title={t('settings.learning.recorded.exposuresBySource')} counts={data.exposures.by_source} />
      <p style={{ margin: 0, color: 'var(--color-muted)' }}>
        {t('settings.learning.recorded.retention', { compact: data.exposures.compact_after_days, keep: data.exposures.keep_days })}
      </p>
      <p style={{ margin: 0, color: 'var(--color-muted)' }}>{t('settings.learning.recorded.repeatedViews')}</p>
    </div>
  )
}
