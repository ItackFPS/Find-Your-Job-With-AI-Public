import type { CSSProperties, ReactNode } from 'react'
import { useState } from 'react'

export interface CardProps {
  /** Optional header title. Omit for a bare content card. */
  title?: ReactNode
  /** Optional small text under the title. */
  subtitle?: ReactNode
  /** Optional controls rendered on the right side of the header (buttons, badges…). */
  actions?: ReactNode
  /** Brighten the border on hover — for cards that act like links/buttons. */
  hoverable?: boolean
  /** Use the elevated surface tone instead of the default one (nested/inset cards). */
  elevated?: boolean
  /** Reduce the default padding (e.g. for tightly packed list containers). */
  compact?: boolean
  style?: CSSProperties
  children?: ReactNode
}

/**
 * Base surface used across the app for grouped content — the single
 * implementation for what was previously a `card` style object copy-pasted
 * in every page (Dashboard, Pipeline, Jobs, JobDetail, Profile).
 */
export function Card({ title, subtitle, actions, hoverable = false, elevated = false, compact = false, style, children }: CardProps) {
  const [hovered, setHovered] = useState(false)

  const baseStyle: CSSProperties = {
    background: elevated ? 'var(--color-surface-elevated)' : 'var(--color-surface)',
    border: `1px solid ${hoverable && hovered ? 'var(--color-accent)' : 'var(--color-border)'}`,
    borderRadius: 'var(--radius-lg)',
    padding: compact ? 'var(--space-4)' : 'var(--space-5)',
    transition: 'border-color var(--motion-duration-fast) var(--motion-ease)',
    ...style,
  }

  const hasHeader = title !== undefined || subtitle !== undefined || actions !== undefined

  return (
    <div
      style={baseStyle}
      onMouseEnter={hoverable ? () => setHovered(true) : undefined}
      onMouseLeave={hoverable ? () => setHovered(false) : undefined}
    >
      {hasHeader && (
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: 'var(--space-2)',
            flexWrap: 'wrap',
            marginBottom: children ? 'var(--space-5)' : 0,
          }}
        >
          <div>
            {title !== undefined && (
              <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: 'var(--font-size-lg)' }}>{title}</div>
            )}
            {subtitle !== undefined && (
              <div style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-sm)', marginTop: '0.2rem' }}>{subtitle}</div>
            )}
          </div>
          {actions !== undefined && (
            <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>{actions}</div>
          )}
        </div>
      )}
      {children}
    </div>
  )
}
