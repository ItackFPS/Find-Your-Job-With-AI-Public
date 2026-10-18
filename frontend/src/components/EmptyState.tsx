import type { ReactNode } from 'react'

export interface EmptyStateProps {
  message: ReactNode
  action?: ReactNode
  icon?: ReactNode
}

export function EmptyState({ message, action, icon }: EmptyStateProps) {
  return (
    <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--color-muted)' }}>
      {icon && <div style={{ marginBottom: 'var(--space-3)' }}>{icon}</div>}
      <div>{message}</div>
      {action && <div style={{ marginTop: 'var(--space-3)' }}>{action}</div>}
    </div>
  )
}
