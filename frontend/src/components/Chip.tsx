import type { ReactNode } from 'react'

export type ChipTone = 'default' | 'accent' | 'success' | 'warning' | 'error' | 'info'

const TONE_STYLES: Record<ChipTone, { bg: string; color: string; border: string }> = {
  default: { bg: 'var(--color-bg)', color: 'var(--color-muted)', border: 'var(--color-border)' },
  accent: { bg: 'var(--color-accent)22', color: 'var(--color-accent)', border: 'var(--color-accent)44' },
  success: { bg: 'var(--color-success)22', color: 'var(--color-success)', border: 'var(--color-success)44' },
  warning: { bg: 'var(--color-warning)22', color: 'var(--color-warning)', border: 'var(--color-warning)44' },
  error: { bg: 'var(--color-error)22', color: 'var(--color-error)', border: 'var(--color-error)44' },
  info: { bg: 'var(--color-info)22', color: 'var(--color-info)', border: 'var(--color-info)44' },
}

export interface ChipProps {
  children: ReactNode
  tone?: ChipTone
  title?: string
}

/**
 * Renders a supplied label as a pill — for skills, locations, sources,
 * seniority, employment type, etc. It never decides whether a job
 * qualifies for anything; it only displays the value it's given.
 */
export function Chip({ children, tone = 'default', title }: ChipProps) {
  const s = TONE_STYLES[tone]
  return (
    <span
      title={title}
      style={{
        display: 'inline-block',
        fontSize: 'var(--font-size-xs)',
        background: s.bg,
        color: s.color,
        border: `1px solid ${s.border}`,
        borderRadius: 'var(--radius-pill)',
        padding: '0.1rem 0.6rem',
      }}
    >
      {children}
    </span>
  )
}
