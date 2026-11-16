import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { ListStatus } from '../api/client'

const line = { margin: 0, fontSize: '0.85rem' }

// Literal keys keep the catalogue check able to see them.
function reasonText(t: (key: string, o?: Record<string, unknown>) => string, name: string, reason: string): string {
  switch (reason) {
    case 'failed': return t('listStatus.reason.failed', { name })
    case 'partial': return t('listStatus.reason.partial', { name })
    case 'network_outage': return t('listStatus.reason.network_outage', { name })
    case 'suspect_empty': return t('listStatus.reason.suspect_empty', { name })
    default: return name
  }
}

/** Why the main list holds what it holds (issue #253): the previous list kept, sources that came
 *  back incomplete, offers carried from earlier searches, offers hidden because their source is off,
 *  and expired offers. Renders nothing when there is nothing to explain. */
export function ListStatusNotice({ status }: { status: ListStatus | null | undefined }) {
  const { t } = useTranslation()
  if (!status) return null
  const incomplete = Object.entries(status.incomplete_sources ?? {})
  const carried = Object.values(status.carried_by_source ?? {}).reduce((a, b) => a + b, 0)
  const restored = Object.entries(status.restored_by_source ?? {})
  const restoredTotal = restored.reduce((a, [, n]) => a + n, 0)
  const off = Object.entries(status.source_off ?? {})
  const offTotal = off.reduce((a, [, n]) => a + n, 0)
  const expired = Object.values(status.expired ?? {}).reduce((a, b) => a + b, 0)
  const stale = status.expired?.stale ?? 0
  if (!status.previous_list_kept && incomplete.length === 0 && carried === 0 && restoredTotal === 0 && offTotal === 0
    && expired === 0) return null
  return (
    <div
      role="status"
      data-testid="list-status"
      style={{
        border: '1px solid var(--color-border)', borderRadius: 'var(--radius-sm)', padding: '0.6rem 0.8rem',
        marginBottom: '1rem', display: 'flex', flexDirection: 'column', gap: '0.3rem', background: 'var(--color-surface)',
      }}
    >
      {status.previous_list_kept && (
        <p style={{ ...line, color: 'var(--color-warning)' }} data-testid="list-status-kept">
          {status.latest_run_status === 'failed' ? t('listStatus.keptFailed') : t('listStatus.keptNoSource')}
        </p>
      )}
      {incomplete.length > 0 && (
        <p style={line} data-testid="list-status-incomplete">
          {t('listStatus.incomplete', {
            sources: incomplete.map(([name, reason]) => reasonText(t, name, reason)).join(', '),
          })}
        </p>
      )}
      {carried > 0 && (
        <p style={{ ...line, color: 'var(--color-muted)' }} data-testid="list-status-carried">
          {t('listStatus.carried', { count: carried })}
        </p>
      )}
      {restoredTotal > 0 && (
        <p style={{ ...line, color: 'var(--color-muted)' }} data-testid="list-status-restored">
          {t('listStatus.restored', { count: restoredTotal, sources: restored.map(([name]) => name).join(', ') })}
        </p>
      )}
      {offTotal > 0 && (
        <p style={{ ...line, color: 'var(--color-muted)' }} data-testid="list-status-off">
          {t('listStatus.sourceOff', { count: offTotal, sources: off.map(([name]) => name).join(', ') })}{' '}
          <Link to="/settings" style={{ color: 'var(--color-accent)' }}>{t('listStatus.settings')}</Link>
        </p>
      )}
      {expired > 0 && (
        <p style={{ ...line, color: 'var(--color-muted)' }} data-testid="list-status-expired">
          {t('listStatus.expired', { count: expired, stale })}
        </p>
      )}
    </div>
  )
}
