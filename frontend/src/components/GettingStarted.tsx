import { Link } from 'react-router-dom'
import { Trans, useTranslation } from 'react-i18next'
import { Card } from './Card'

// First-run guide, shown on the Dashboard while the database has no job yet.
// Mirrors docs/windows-installer.md ("First run") and config/sources.yaml: keep the
// two source lists in step with the shipped configuration.
const NO_ACCOUNT = ['France Travail', 'EURES']
const SCANNERS = ['LinkedIn', 'HelloWork']

const item = { marginBottom: 'var(--space-3)', lineHeight: 1.5 }

export function GettingStarted() {
  const { t } = useTranslation()
  const b = <strong />
  return (
    <Card title={t('components.gettingStarted.title')} style={{ marginBottom: 'var(--space-4)' }}>
      <p style={{ marginTop: 0, color: 'var(--color-muted)' }}>
        {t('components.gettingStarted.intro')}
      </p>
      <ol style={{ paddingLeft: '1.25rem', margin: 0 }}>
        <li style={item}>
          <Trans i18nKey="components.gettingStarted.profile" components={{ profile: <Link to="/profile" style={{ color: 'var(--color-accent)' }} /> }} />
        </li>
        <li style={item}>
          <Trans
            i18nKey="components.gettingStarted.sources"
            values={{ noAccount: NO_ACCOUNT.join(', '), scanners: SCANNERS.join(t('components.gettingStarted.and')) }}
            components={{ b }}
          />
        </li>
        <li style={item}>
          <Trans i18nKey="components.gettingStarted.run" components={{ b }} />
        </li>
      </ol>
      <p style={{ marginBottom: 0, color: 'var(--color-muted)' }}>
        {t('components.gettingStarted.scope')}
      </p>
    </Card>
  )
}
