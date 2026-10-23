import type { ReactNode } from 'react'

export type ChipTone = 'default' | 'accent' | 'success' | 'warning' | 'error' | 'info'

const TONE_STYLES: Record<ChipTone, { bg: string; color: string; border: string }> = {
  default: { bg: 'var(--color-bg)', color: 'var(--color-muted)', border: 'var(--color-border)' },
  accent: { bg: 'color-mix(in srgb, var(--color-accent) 13%, transparent)', color: 'var(--color-accent)', border: 'color-mix(in srgb, var(--color-accent) 27%, transparent)' },
  success: { bg: 'color-mix(in srgb, var(--color-success) 13%, transparent)', color: 'var(--color-success)', border: 'color-mix(in srgb, var(--color-success) 27%, transparent)' },
  warning: { bg: 'color-mix(in srgb, var(--color-warning) 13%, transparent)', color: 'var(--color-warning)', border: 'color-mix(in srgb, var(--color-warning) 27%, transparent)' },
  error: { bg: 'color-mix(in srgb, var(--color-error) 13%, transparent)', color: 'var(--color-error)', border: 'color-mix(in srgb, var(--color-error) 27%, transparent)' },
  info: { bg: 'color-mix(in srgb, var(--color-info) 13%, transparent)', color: 'var(--color-info)', border: 'color-mix(in srgb, var(--color-info) 27%, transparent)' },
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
