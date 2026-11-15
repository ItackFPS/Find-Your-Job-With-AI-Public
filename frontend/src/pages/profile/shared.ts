

export const inputStyle: React.CSSProperties = {
  background: 'var(--color-bg)',
  border: '1px solid var(--color-border)',
  borderRadius: '7px',
  padding: '0.5rem 0.8rem',
  color: 'var(--color-text)',
  fontSize: '0.9rem',
  outline: 'none',
  width: '100%',
  boxSizing: 'border-box',
}

export const label: React.CSSProperties = {
  fontSize: '0.8rem',
  fontWeight: 600,
  color: 'var(--color-muted)',
  marginBottom: '0.6rem',
  display: 'flex',
  alignItems: 'baseline',
  gap: '0.45rem',
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
}

export const fieldGroup: React.CSSProperties = {
  marginBottom: '1.4rem',
}

export const sectionDivider: React.CSSProperties = {
  border: 'none',
  borderTop: '1px solid var(--color-border)',
  margin: '1.5rem 0',
}

export type TagTone = 'language' | 'default'

export const TAG_TONE_STYLES: Record<TagTone, { border: string }> = {
  language: { border: 'color-mix(in srgb, var(--color-info) 40%, transparent)' },
  default: { border: 'var(--color-border)' },
}

export function mergeFieldStyle(focused: boolean): React.CSSProperties {
  return {
    background: 'var(--color-bg)',
    border: `1px solid ${focused ? 'var(--color-accent)' : 'var(--color-border)'}`,
    borderRadius: '9px',
    padding: '0.6rem',
    display: 'flex',
    flexWrap: 'wrap',
    gap: '0.5rem',
    alignItems: 'center',
    minHeight: '48px',
    boxShadow: focused ? '0 0 0 3px color-mix(in srgb, var(--color-accent) 13%, transparent)' : undefined,
    transition: 'border-color 0.15s, box-shadow 0.15s',
    position: 'relative',
  }
}

export const mergeInputStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  outline: 'none',
  color: 'var(--color-text)',
  fontSize: '0.9rem',
  flex: 1,
  minWidth: '160px',
  padding: '0.3rem 0.2rem',
}

export const chipRemoveStyle: React.CSSProperties = {
  background: 'var(--color-surface-elevated)',
  border: 'none',
  borderRadius: '50%',
  color: 'var(--color-muted)',
  cursor: 'pointer',
  width: '18px',
  height: '18px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: '0.85rem',
  lineHeight: 1,
  padding: 0,
  flex: 'none',
}

export const dropdownStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 6px)',
  left: 0,
  right: 0,
  background: 'var(--color-surface)',
  border: '1px solid var(--color-border)',
  borderRadius: '9px',
  boxShadow: 'var(--shadow-lg)',
  zIndex: 100,
  overflow: 'hidden',
}

export const iconButtonStyle: React.CSSProperties = {
  background: 'var(--color-bg)',
  border: '1px solid var(--color-border)',
  borderRadius: '6px',
  width: '1.8rem',
  height: '1.8rem',
  fontSize: '0.85rem',
  color: 'var(--color-text)',
  cursor: 'pointer',
  flexShrink: 0,
}

// Prevents "Docker" and "docker" (or "French"/"french") coexisting as two
// separate entries — an exact-string check alone lets that through.
export function includesCaseInsensitive(values: string[], candidate: string): boolean {
  const target = candidate.toLowerCase()
  return values.some(v => v.toLowerCase() === target)
}
