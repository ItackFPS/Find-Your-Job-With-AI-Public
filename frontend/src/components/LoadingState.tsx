import { useTranslation } from 'react-i18next'

export interface LoadingStateProps {
  message?: string
  /** Full-page loading block (2rem padding) vs. an inline compact line. */
  compact?: boolean
}

export function LoadingState({ message, compact = false }: LoadingStateProps) {
  const { t } = useTranslation()
  return (
    <div
      role="status"
      style={{
        color: 'var(--color-muted)',
        padding: compact ? 0 : 'var(--space-8)',
        fontSize: compact ? 'var(--font-size-sm)' : undefined,
      }}
    >
      {message ?? t('common.loading')}
    </div>
  )
}
