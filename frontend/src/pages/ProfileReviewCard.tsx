import { useTranslation } from 'react-i18next'
import { Button, Card } from '../components'
import type { Profile } from '../api/client'

/** The one-time "we updated your profile, please check" screen (profile v2, F5.1).
 *  Shown while ``profile.review.pending``; confirming saves the profile with it cleared. */
export function ProfileReviewCard({ profile, onConfirm, saving }: {
  profile: Profile
  onConfirm: () => void
  saving: boolean
}) {
  const { t } = useTranslation()
  const review = profile.review
  if (!review?.pending) return null
  const levels = profile.targets?.levels ?? []
  const held = (profile.education ?? []).filter(e => e.level !== null && !e.in_progress).map(e => e.level as number)
  const top = held.length ? Math.max(...held) : null

  return (
    <Card style={{ marginBottom: '1rem', borderColor: 'var(--color-accent)' }} title={t('profile.review.title')}
      subtitle={t('profile.review.intro')}>
      <div role="region" aria-label={t('profile.review.title')}>
        <dl style={{ display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '0.35rem 1rem', margin: '0 0 0.75rem' }}>
          <dt style={{ fontWeight: 600 }}>{t('profile.review.targetLevels')}</dt>
          <dd style={{ margin: 0 }}>
            {levels.length ? levels.map(l => t(`profile.review.levels.${l}`)).join(', ') : t('profile.review.noLevels')}
          </dd>
          <dt style={{ fontWeight: 600 }}>{t('profile.review.educationLevel')}</dt>
          <dd style={{ margin: 0 }}>
            {top !== null ? t('profile.review.levelValue', { level: top }) : t('profile.review.noEducation')}
          </dd>
        </dl>
        {review.notes.length > 0 && (
          <>
            <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>{t('profile.review.changes')}</div>
            <ul style={{ margin: '0 0 0.75rem', paddingLeft: '1.2rem' }}>
              {review.notes.map(n => <li key={n}>{n}</li>)}
            </ul>
          </>
        )}
        <Button variant="primary" onClick={onConfirm} disabled={saving}>
          {saving ? t('profile.review.confirming') : t('profile.review.confirm')}
        </Button>
      </div>
    </Card>
  )
}
