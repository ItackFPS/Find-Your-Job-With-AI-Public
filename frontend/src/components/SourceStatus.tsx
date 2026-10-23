import { StatusBadge } from './StatusBadge'

// One source's collection outcome in a run: completed, partial (errors but jobs came
// back), failed, or skipped (left out before collecting, e.g. no credentials), with
// the reason it gives (a skip, a failure, the first error of a partial run). Runs recorded
// before the status existed show "-".
export function SourceStatus({ status, reason }: { status?: string | null; reason?: string | null }) {
  if (!status) return <span style={{ color: 'var(--color-muted)' }}>-</span>
  return (
    <span title={reason ?? undefined}>
      <StatusBadge status={status} />
      {reason && (
        <div style={{ marginTop: '0.25rem', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', whiteSpace: 'normal', maxWidth: '28rem' }}>
          {reason}
        </div>
      )}
    </span>
  )
}
