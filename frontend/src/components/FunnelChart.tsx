import { Fragment } from 'react'

export interface FunnelRow {
  key: string
  label: string
  /** Missing on runs recorded before this stat existed — rendered as "–", not 0. */
  value: number | undefined
  max: number
  /** Optional small uppercase section header rendered above this row. */
  groupLabel?: string
  /** Optional small callout rendered under this row (e.g. a stage-drop note). */
  note?: string
}

export interface FunnelChartProps {
  rows: FunnelRow[]
  /** Show the value's percentage of `max` next to the bar. */
  showPercent?: boolean
  labelWidth?: string
}

/**
 * Renders an already-computed sequence of stage rows as label + bar + value.
 * It never decides what counts as a stage or recalculates any number —
 * callers (Dashboard/Pipeline) own that and simply pass the rows.
 */
export function FunnelChart({ rows, showPercent = false, labelWidth = '160px' }: FunnelChartProps) {
  return (
    <div>
      {rows.map(row => {
        const hasValue = typeof row.value === 'number'
        const pct = hasValue && row.max > 0 ? Math.round((row.value! / row.max) * 100) : 0

        return (
          <Fragment key={row.key}>
            {row.groupLabel && (
              <div
                style={{
                  fontSize: 'var(--font-size-xs)',
                  fontWeight: 'var(--font-weight-semibold)',
                  color: 'var(--color-muted)',
                  letterSpacing: '0.05em',
                  margin: '0.6rem 0 0.3rem',
                }}
              >
                {row.groupLabel}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', marginBottom: '0.55rem' }}>
              <span style={{ width: labelWidth, fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)', flexShrink: 0 }}>
                {row.label}
              </span>
              <div
                role="progressbar"
                aria-label={row.label}
                aria-valuenow={hasValue ? pct : undefined}
                aria-valuemin={0}
                aria-valuemax={100}
                style={{ flex: 1, background: 'var(--color-bg)', borderRadius: 'var(--radius-sm)', height: '8px', overflow: 'hidden' }}
              >
                <div
                  style={{
                    width: `${pct}%`,
                    height: '100%',
                    background: 'var(--color-accent)',
                    borderRadius: 'var(--radius-sm)',
                    transition: 'width var(--motion-duration-slow) var(--motion-ease)',
                  }}
                />
              </div>
              <span style={{ width: '60px', textAlign: 'right', fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-sm)' }}>
                {hasValue ? row.value!.toLocaleString() : '–'}
              </span>
              {showPercent && (
                <span style={{ width: '36px', textAlign: 'right', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>
                  {hasValue ? `${pct}%` : ''}
                </span>
              )}
            </div>
            {row.note && (
              <div style={{
                marginLeft: labelWidth,
                marginBottom: '0.5rem',
                display: 'inline-flex',
                fontSize: 'var(--font-size-xs)',
                color: 'var(--color-error)',
                background: 'color-mix(in srgb, var(--color-error) 10%, transparent)',
                border: '1px solid color-mix(in srgb, var(--color-error) 27%, transparent)',
                borderRadius: 'var(--radius-pill)',
                padding: '0.05rem 0.5rem',
              }}>
                {row.note}
              </div>
            )}
          </Fragment>
        )
      })}
    </div>
  )
}
