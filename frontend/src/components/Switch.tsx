import type { CSSProperties, ReactNode } from 'react'

export interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  label?: ReactNode
  description?: ReactNode
}

/**
 * A track/knob toggle, built the same way as this app's other custom
 * controls (CheckboxGroup, SegmentedControl in Profile.tsx) — a plain
 * button rather than a styled native checkbox.
 */
export function Switch({ checked, onChange, disabled = false, label, description }: SwitchProps) {
  const track: CSSProperties = {
    width: '34px',
    height: '19px',
    borderRadius: 'var(--radius-pill)',
    background: checked ? 'var(--color-accent-dim)' : 'var(--color-border)',
    border: 'none',
    padding: 0,
    position: 'relative',
    flex: 'none',
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    transition: 'background 0.15s',
  }

  const knob: CSSProperties = {
    position: 'absolute',
    top: '2px',
    left: checked ? '17px' : '2px',
    width: '15px',
    height: '15px',
    borderRadius: '50%',
    background: checked ? 'var(--color-accent)' : 'var(--color-muted)',
    transition: 'left 0.15s, background 0.15s',
  }

  const control = (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      style={track}
    >
      <span style={knob} />
    </button>
  )

  if (!label && !description) return control

  return (
    <label style={{ display: 'flex', alignItems: 'center', gap: '0.55rem', cursor: disabled ? 'not-allowed' : 'pointer' }}>
      {control}
      <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
        {label && <span style={{ fontSize: 'var(--font-size-sm)', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-text)' }}>{label}</span>}
        {description && <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>{description}</span>}
      </span>
    </label>
  )
}
