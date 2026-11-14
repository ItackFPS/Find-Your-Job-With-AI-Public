import { useTranslation } from 'react-i18next'
import { useCollectionFrozen } from '../hooks/useCollectionFrozen'

/** The notice shown while collection is paused (owner decision D7). Renders nothing otherwise. */
export function CollectionFreezeNotice() {
  const { t } = useTranslation()
  const frozen = useCollectionFrozen()
  if (!frozen) return null
  return (
    <div role="status" style={{
      border: '1px solid var(--color-warning)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)',
      marginBottom: 'var(--space-4)', display: 'flex', flexDirection: 'column', gap: 'var(--space-1)',
    }}>
      <strong>{t('collection.frozen.title')}</strong>
      <span>{t('collection.frozen.body')}</span>
    </div>
  )
}
