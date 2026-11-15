import { useTranslation } from 'react-i18next'
import { StatusBadge } from './StatusBadge'

// One source's collection outcome in a run: completed, partial (errors but jobs came
// back), failed, or skipped (left out before collecting, e.g. no credentials), with
// the reason it gives (a skip, a failure, the first error of a partial run). Runs recorded
// before the status existed show "-". A fixed skip reason with a key is said in the user's
// language; any other reason (an error message, an older run) is shown as recorded.
export function SourceStatus({ status, reason, reasonKey }: {
  status?: string | null; reason?: string | null; reasonKey?: string | null
}) {
  const { t, i18n } = useTranslation()
  if (!status) return <span style={{ color: 'var(--color-muted)' }}>-</span>
  const key = reasonKey ? `sourceStatus.reason.${reasonKey}` : null
  const shown = key && i18n.exists(key) ? t(key) : reason
  return (
    <span title={shown ?? undefined}>
      <StatusBadge status={status} />
      {shown && (
        <div style={{ marginTop: '0.25rem', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', whiteSpace: 'normal', maxWidth: '28rem' }}>
          {shown}
        </div>
      )}
    </span>
  )
}
