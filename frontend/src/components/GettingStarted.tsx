import { Link } from 'react-router-dom'
import { Card } from './Card'

// First-run guide, shown on the Dashboard while the database has no job yet.
// Mirrors docs/windows-installer.md ("First run") and config/sources.yaml: keep the
// two source lists in step with the shipped configuration.
const NO_ACCOUNT = ['RemoteOK', 'Jobicy', 'We Work Remotely', 'EURES']
const SCANNERS = ['LinkedIn', 'HelloWork']
const NEEDS_CREDENTIALS = [
  { name: 'France Travail', vars: 'FRANCE_TRAVAIL_CLIENT_ID and FRANCE_TRAVAIL_CLIENT_SECRET' },
  { name: 'Indeed', vars: 'INDEED_API_KEY' },
]

const item = { marginBottom: 'var(--space-3)', lineHeight: 1.5 }
const code = { fontFamily: 'var(--font-family-mono)', fontSize: 'var(--font-size-sm)' }

export function GettingStarted() {
  return (
    <Card title="Getting started" style={{ marginBottom: 'var(--space-4)' }}>
      <p style={{ marginTop: 0, color: 'var(--color-muted)' }}>
        Nothing is collected until you ask: opening the app starts no scan and no run.
      </p>
      <ol style={{ paddingLeft: '1.25rem', margin: 0 }}>
        <li style={item}>
          Fill in your <Link to="/profile" style={{ color: 'var(--color-accent)' }}>Profile</Link>: target
          roles, skills, locations and languages. Searches are built from it.
        </li>
        <li style={item}>
          Sources that need no account: {NO_ACCOUNT.join(', ')} (collected by <strong>Run pipeline</strong>),
          and {SCANNERS.join(' and ')} (<strong>Scan now</strong> on their cards below).
        </li>
        <li style={item}>
          Optional sources, skipped until you add their credentials:
          <ul style={{ paddingLeft: '1.25rem', margin: 'var(--space-1) 0 0' }}>
            {NEEDS_CREDENTIALS.map(s => (
              <li key={s.name}>{s.name}: <span style={code}>{s.vars}</span></li>
            ))}
          </ul>
          Copy <span style={code}>.env.example</span> to <span style={code}>.env</span> in the
          application folder, fill in only those lines, then restart the app.
        </li>
        <li style={item}>
          Press <strong>Run pipeline</strong>. The first run downloads the local embedding model
          (about 470 MB) once. Local AI is off; no job data is sent to a cloud AI service.
        </li>
      </ol>
    </Card>
  )
}
