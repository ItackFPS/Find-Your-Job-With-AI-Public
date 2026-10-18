import type { ReactNode } from 'react'

export interface RingProps {
  /** 0-100. */
  value: number
  size?: number
  strokeWidth?: number
  color?: string
  trackColor?: string
  children?: ReactNode
}

/**
 * A circular percentage gauge. Purely presentational — it draws whatever
 * `value` it's given and never computes a score or percentage itself.
 */
export function Ring({ value, size = 56, strokeWidth = 6, color = 'var(--color-accent)', trackColor = 'var(--color-bg)', children }: RingProps) {
  const pct = Math.max(0, Math.min(100, value))
  const r = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * r
  const offset = circumference * (1 - pct / 100)
  const center = size / 2

  return (
    <div style={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={center} cy={center} r={r} fill="none" stroke={trackColor} strokeWidth={strokeWidth} />
        <circle
          cx={center}
          cy={center}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform={`rotate(-90 ${center} ${center})`}
          style={{ transition: 'stroke-dashoffset 0.4s ease' }}
        />
      </svg>
      {children !== undefined && (
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: size <= 40 ? 'var(--font-size-xs)' : 'var(--font-size-sm)',
          fontWeight: 'var(--font-weight-bold)',
        }}>
          {children}
        </div>
      )}
    </div>
  )
}
