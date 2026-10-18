export interface LoadingStateProps {
  message?: string
  /** Full-page loading block (2rem padding) vs. an inline compact line. */
  compact?: boolean
}

export function LoadingState({ message = 'Loading…', compact = false }: LoadingStateProps) {
  return (
    <div
      role="status"
      style={{
        color: 'var(--color-muted)',
        padding: compact ? 0 : '2rem',
        fontSize: compact ? 'var(--font-size-sm)' : undefined,
      }}
    >
      {message}
    </div>
  )
}
