import { useTranslation } from 'react-i18next'
import { BackgroundCard, DailyRunsCard, DataCard, SourcesCard } from '../components/settings'

// Settings › Sources and background (issue #82, owner decisions R-Q3, R-C2, R-C5):
// everything about collection, without a terminal or a settings file.
export default function Settings() {
  const { t } = useTranslation()
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)', maxWidth: '60rem' }}>
      <div>
        <h1 style={{ marginBottom: 'var(--space-1)' }}>{t('settings.title')}</h1>
        <p style={{ margin: 0, color: 'var(--color-muted)' }}>{t('settings.subtitle')}</p>
      </div>
      <BackgroundCard />
      <DailyRunsCard />
      <SourcesCard />
      <DataCard />
    </div>
  )
}
