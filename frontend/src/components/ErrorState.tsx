import type { ReactNode } from 'react'
import { Button } from './Button'

export interface ErrorStateProps {
  /** What went wrong, in plain words. */
  message: ReactNode
  /** Optional technical detail (status code, server text), shown smaller. */
  details?: ReactNode
  /** The recovery action. Omit only when there is genuinely nothing the user can do. */
  onRetry?: () => void
  retryLabel?: string
}

/**
 * Error block with a recovery action (§C13): every failed load offers a way forward
 * instead of a dead end. Announced to assistive technology as an alert.
 */
export function ErrorState({ message, details, onRetry, retryLabel = 'Try again' }: ErrorStateProps) {
  return (
    <div
      role="alert"
      style={{
        border: '1px solid color-mix(in srgb, var(--color-danger) 40%, transparent)',
        background: 'color-mix(in srgb, var(--color-danger) 6%, var(--color-surface))',
        borderRadius: 'var(--radius-lg)',
        padding: 'var(--space-5)',
        color: 'var(--color-text)',
      }}
    >
      <div style={{ fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-danger)' }}>{message}</div>
      {details !== undefined && (
        <div style={{ marginTop: 'var(--space-1)', fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{details}</div>
      )}
      {onRetry && (
        <div style={{ marginTop: 'var(--space-3)' }}>
          <Button variant="secondary" onClick={onRetry}>{retryLabel}</Button>
        </div>
      )}
    </div>
  )
}
