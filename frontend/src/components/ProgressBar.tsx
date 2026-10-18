export type ProgressBarTone = 'accent' | 'success' | 'warning' | 'error'

const TONE_COLORS: Record<ProgressBarTone, string> = {
  accent: 'var(--color-accent)',
  success: 'var(--color-success)',
  warning: 'var(--color-warning)',
  error: 'var(--color-error)',
}

export interface ProgressBarProps {
  /** 0-100. Omit (or pass `indeterminate`) when there's no known total yet. */
  value?: number
  indeterminate?: boolean
  label?: string
  rightLabel?: string
  height?: string
  tone?: ProgressBarTone
}

/**
 * Generic percentage bar. Renders whatever value/label it's given — it
 * never computes pipeline stages or stats itself.
 */
export function ProgressBar({ value, indeterminate = false, label, rightLabel, height = '8px', tone = 'accent' }: ProgressBarProps) {
  const pct = indeterminate ? undefined : Math.max(0, Math.min(100, value ?? 0))
  const color = TONE_COLORS[tone]

  return (
    <div>
      {(label || rightLabel) && (
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: 'var(--font-size-sm)',
            color: 'var(--color-muted)',
            marginBottom: '0.3rem',
          }}
        >
          {label && <span>{label}</span>}
          {rightLabel && <span>{rightLabel}</span>}
        </div>
      )}
      <div
        role="progressbar"
        aria-valuenow={indeterminate ? undefined : pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-busy={indeterminate || undefined}
        aria-label={label}
        style={{
          background: 'var(--color-bg)',
          borderRadius: 'var(--radius-sm)',
          height,
          overflow: 'hidden',
          position: 'relative',
        }}
      >
        {indeterminate ? (
          <div
            style={{
              position: 'absolute',
              top: 0,
              bottom: 0,
              width: '30%',
              background: color,
              borderRadius: 'var(--radius-sm)',
              animation: 'fyj-indeterminate 1.2s ease-in-out infinite',
            }}
          />
        ) : (
          <div
            style={{
              width: `${pct}%`,
              height: '100%',
              background: color,
              borderRadius: 'var(--radius-sm)',
              // Slower, decelerating transition so a big jump between two
              // SSE updates (e.g. a stage that reports rarely) reads as a
              // smooth ramp rather than an instant snap.
              transition: 'width 0.8s cubic-bezier(0.22, 1, 0.36, 1)',
            }}
          />
        )}
      </div>
    </div>
  )
}
