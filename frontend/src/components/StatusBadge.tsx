// Colors for the run/scan statuses actually produced by the backend
// (PipelineRun.status / LinkedInScanRun.status / HelloWorkScanRun.status —
// see src/api/schemas.py). Not a general-purpose status enum: unknown
// strings fall back to the muted color rather than inventing a new state.
const STATUS_COLORS: Record<string, string> = {
  completed: 'var(--color-green)',
  failed: 'var(--color-red)',
  running: 'var(--color-yellow)',
  // Scanner pause/resume states (issue #81, scan_state in src/scan_control.py).
  pausing: 'var(--color-yellow)',
  paused: 'var(--color-info)',
  interrupted: 'var(--color-info)',
  // Per-source collection outcome (SourceStats.status, src/pipeline/orchestrator.py).
  partial: 'var(--color-yellow)',
  skipped: 'var(--color-info)',
}

export interface StatusBadgeProps {
  status: string
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const color = STATUS_COLORS[status] || 'var(--color-muted)'
  return (
    <span
      style={{
        background: `${color}22`,
        color,
        border: `1px solid ${color}44`,
        borderRadius: 'var(--radius-pill)',
        padding: '0.15rem 0.65rem',
        fontSize: 'var(--font-size-xs)',
        fontWeight: 'var(--font-weight-semibold)',
        textTransform: 'capitalize',
      }}
    >
      {status}
    </span>
  )
}
