import type { ReactNode } from 'react'

export type StatTileTone = 'default' | 'success' | 'warning' | 'error' | 'info' | 'accent'

const TONE_COLORS: Record<StatTileTone, string> = {
  default: 'var(--color-text)',
  success: 'var(--color-success)',
  warning: 'var(--color-warning)',
  error: 'var(--color-error)',
  info: 'var(--color-info)',
  accent: 'var(--color-accent)',
}

export interface StatTileProps {
  label: string
  /** The value to display. This component only renders it — it never computes stats. */
  value: number | string | null | undefined
  secondaryText?: ReactNode
  icon?: ReactNode
  tone?: StatTileTone
  loading?: boolean
}

export function StatTile({ label, value, secondaryText, icon, tone = 'default', loading = false }: StatTileProps) {
  return (
    <div
      style={{
        background: 'var(--color-bg)',
        border: '1px solid var(--color-border)',
        borderRadius: 'var(--radius-md)',
        padding: '1rem 1.25rem',
        minWidth: '120px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
        {icon}
        {loading ? (
          <div
            role="status"
            aria-label={`Loading ${label}`}
            style={{
              width: '3rem',
              height: '1.65rem',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--color-border)',
              opacity: 0.6,
            }}
          />
        ) : (
          <div style={{ fontSize: '1.65rem', fontWeight: 'var(--font-weight-bold)', color: TONE_COLORS[tone] }}>
            {typeof value === 'number' ? value.toLocaleString() : (value ?? '–')}
          </div>
        )}
      </div>
      <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginTop: '0.2rem' }}>{label}</div>
      {secondaryText !== undefined && (
        <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginTop: '0.1rem' }}>{secondaryText}</div>
      )}
    </div>
  )
}
