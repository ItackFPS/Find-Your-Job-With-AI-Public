import { Link } from 'react-router-dom'
import { Trans, useTranslation } from 'react-i18next'
import { Card } from './Card'

// First-run guide, shown on the Dashboard while the database has no job yet.
// Mirrors docs/windows-installer.md ("First run") and config/sources.yaml: keep the
// two source lists in step with the shipped configuration.
const NO_ACCOUNT = ['RemoteOK', 'Jobicy', 'We Work Remotely', 'EURES']
const SCANNERS = ['LinkedIn', 'HelloWork']
const NEEDS_CREDENTIALS = [
  { name: 'France Travail', vars: ['FRANCE_TRAVAIL_CLIENT_ID', 'FRANCE_TRAVAIL_CLIENT_SECRET'] },
]

const item = { marginBottom: 'var(--space-3)', lineHeight: 1.5 }
const code = { fontFamily: 'var(--font-family-mono)', fontSize: 'var(--font-size-sm)' }

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
          {t('components.gettingStarted.optional')}
          <ul style={{ paddingLeft: '1.25rem', margin: 'var(--space-1) 0 0' }}>
            {NEEDS_CREDENTIALS.map(s => (
              <li key={s.name}>{s.name}: {s.vars.map((v, i) => <span key={v}>{i > 0 && t('components.gettingStarted.envVarsJoin')}<code style={code}>{v}</code></span>)}</li>
            ))}
          </ul>
          <Trans
            i18nKey="components.gettingStarted.envFile"
            components={{ example: <code style={code}>.env.example</code>, env: <code style={code}>.env</code> }}
          />
        </li>
        <li style={item}>
          <Trans i18nKey="components.gettingStarted.run" components={{ b }} />
        </li>
      </ol>
    </Card>
  )
}
