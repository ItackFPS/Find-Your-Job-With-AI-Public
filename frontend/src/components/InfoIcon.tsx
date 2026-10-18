import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export interface InfoIconProps {
  /** Accessible name for the trigger button. */
  label?: string
  /**
   * Tooltip/popover content. This phase only ships the interaction
   * primitive — actual metric-explanation copy is wired up in a later
   * phase — so `children` is optional and the icon renders inert (no
   * popover) when omitted.
   */
  children?: ReactNode
}

/**
 * Accessible, keyboard-operable info affordance. Renders a small "ⓘ"
 * button that reveals `children` in a popover on hover, click, or focus.
 * No backend dependency and no business logic — later phases decide what
 * content goes inside.
 */
export function InfoIcon({ label = 'More information', children }: InfoIconProps) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLSpanElement>(null)
  const popoverId = useId()
  const hasContent = children !== undefined

  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    const onClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('mousedown', onClickOutside)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('mousedown', onClickOutside)
    }
  }, [open])

  return (
    <span
      ref={containerRef}
      style={{ position: 'relative', display: 'inline-flex', verticalAlign: 'middle' }}
      onMouseEnter={() => hasContent && setOpen(true)}
      onMouseLeave={() => hasContent && setOpen(false)}
    >
      <button
        type="button"
        aria-label={label}
        aria-expanded={hasContent ? open : undefined}
        aria-describedby={hasContent && open ? popoverId : undefined}
        onClick={() => hasContent && setOpen(o => !o)}
        onFocus={() => hasContent && setOpen(true)}
        onBlur={() => setOpen(false)}
        disabled={!hasContent}
        style={{
          width: '16px',
          height: '16px',
          padding: 0,
          borderRadius: '50%',
          border: '1px solid var(--color-border)',
          background: 'var(--color-bg)',
          color: 'var(--color-muted)',
          fontSize: '0.7rem',
          lineHeight: 1,
          cursor: hasContent ? 'pointer' : 'default',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        i
      </button>

      {hasContent && open && (
        <div
          id={popoverId}
          role="tooltip"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            zIndex: 100,
            minWidth: '220px',
            maxWidth: '320px',
            background: 'var(--color-surface-elevated)',
            border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius-md)',
            boxShadow: 'var(--shadow-md)',
            padding: 'var(--space-3)',
            fontSize: 'var(--font-size-sm)',
            color: 'var(--color-text)',
            fontWeight: 'var(--font-weight-normal)',
            textTransform: 'none',
          }}
        >
          {children}
        </div>
      )}
    </span>
  )
}
