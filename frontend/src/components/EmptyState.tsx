import type { ReactNode } from 'react'

export interface EmptyStateProps {
  message: ReactNode
  action?: ReactNode
  icon?: ReactNode
}

export function EmptyState({ message, action, icon }: EmptyStateProps) {
  return (
    <div style={{ textAlign: 'center', padding: 'var(--space-12)', color: 'var(--color-muted)' }}>
      {icon && <div style={{ marginBottom: 'var(--space-3)' }}>{icon}</div>}
      <div>{message}</div>
      {action && <div style={{ marginTop: 'var(--space-3)' }}>{action}</div>}
    </div>
  )
}
