import { NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getNotificationStatus } from '../api/client'
import { httpUrl } from '../utils'

const NAV_ITEMS: { to: string; label: string; icon: React.ReactNode; end?: boolean }[] = [
  {
    to: '/', label: 'Dashboard', end: true,
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <rect x="2" y="2" width="6" height="7" rx="1.2" />
        <rect x="10" y="2" width="6" height="4" rx="1.2" />
        <rect x="10" y="8" width="6" height="8" rx="1.2" />
        <rect x="2" y="11" width="6" height="5" rx="1.2" />
      </svg>
    ),
  },
  {
    to: '/jobs', label: 'Jobs',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <rect x="2.5" y="5" width="13" height="9.5" rx="1.4" />
        <path d="M6.5 5V3.8A1.3 1.3 0 0 1 7.8 2.5h2.4A1.3 1.3 0 0 1 11.5 3.8V5" />
        <path d="M2.5 9h13" />
      </svg>
    ),
  },
  {
    to: '/review', label: 'Review',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M3 4.5h12M3 9h7M3 13.5h5" strokeLinecap="round" />
        <circle cx="13.5" cy="12.5" r="2.5" />
        <path d="m15.3 14.3 1.4 1.4" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    to: '/profile', label: 'Profile',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <circle cx="9" cy="6" r="3" />
        <path d="M2.8 15.5a6.2 6.2 0 0 1 12.4 0" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    to: '/stats', label: 'Stats',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor" stroke="none">
        <rect x="2" y="8" width="3.4" height="8" rx="0.8" />
        <rect x="7.3" y="3" width="3.4" height="13" rx="0.8" />
        <rect x="12.6" y="10.5" width="3.4" height="5.5" rx="0.8" />
      </svg>
    ),
  },
  {
    to: '/skills', label: 'Skills',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <circle cx="4.5" cy="4.5" r="2" />
        <circle cx="13.5" cy="5.5" r="2" />
        <circle cx="9" cy="13.5" r="2" />
        <path d="M6.4 5 11.5 5.3M5.6 6.3 8 11.8M12.6 7.3 10 11.7" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    to: '/lab', label: 'Lab',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M7 2.5h4M7.8 2.5v4.6L3.6 14a1.2 1.2 0 0 0 1 1.8h8.8a1.2 1.2 0 0 0 1-1.8l-4.2-6.9V2.5" strokeLinejoin="round" />
        <path d="M5.3 11h7.4" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    to: '/pipeline', label: 'Pipeline',
    icon: (
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.6">
        <circle cx="4" cy="4" r="2.2" />
        <circle cx="14" cy="9" r="2.2" />
        <circle cx="4" cy="14" r="2.2" />
        <path d="M6.2 5.2 11.8 8" strokeLinecap="round" />
        <path d="M11.8 10 6.2 12.8" strokeLinecap="round" />
      </svg>
    ),
  },
]

function StatusRow({ label, connected }: { label: string; connected: boolean | undefined }) {
  const color = connected === undefined ? 'var(--color-muted)' : connected ? 'var(--color-success)' : 'var(--color-error)'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)' }}>
      <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: color, flexShrink: 0 }} />
      {label}
    </div>
  )
}

export function Sidebar() {
  const { data: notifStatus, isError } = useQuery({
    queryKey: ['notification-status'],
    queryFn: getNotificationStatus,
    refetchInterval: 30000,
    retry: false,
  })

  const backendConnected = isError ? false : notifStatus !== undefined ? true : undefined
  const n8nReachable = notifStatus?.providers?.n8n?.reachable
  const discordServerUrl = httpUrl(notifStatus?.discord_server_url)

  return (
    <div style={{
      width: '220px',
      flexShrink: 0,
      background: 'var(--color-surface)',
      borderRight: '1px solid var(--color-border)',
      display: 'flex',
      flexDirection: 'column',
      height: '100vh',
      position: 'sticky',
      top: 0,
    }}>
      <div style={{ padding: '1.25rem 1.25rem 1rem', fontWeight: 'var(--font-weight-bold)', color: 'var(--color-accent)', fontSize: 'var(--font-size-lg)' }}>
        Find Your Job
      </div>

      <nav style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem', padding: '0 0.75rem', flex: 1 }}>
        {NAV_ITEMS.map(({ to, label, icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            style={({ isActive }) => ({
              display: 'flex',
              alignItems: 'center',
              gap: 'var(--space-3)',
              padding: '0.55rem 0.75rem',
              borderRadius: 'var(--radius-md)',
              textDecoration: 'none',
              fontSize: 'var(--font-size-base)',
              color: isActive ? '#fff' : 'var(--color-muted)',
              background: isActive ? 'var(--color-accent)' : 'transparent',
              fontWeight: isActive ? 'var(--font-weight-semibold)' : 'var(--font-weight-normal)',
              transition: 'all 0.1s',
            })}
          >
            {icon}
            {label}
          </NavLink>
        ))}
      </nav>

      <div style={{ padding: '0.75rem', display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
        {discordServerUrl && <a
          href={discordServerUrl}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 'var(--space-2)',
            padding: '0.5rem 0.75rem',
            borderRadius: 'var(--radius-md)',
            textDecoration: 'none',
            color: 'var(--color-muted)',
            fontSize: 'var(--font-size-sm)',
            border: '1px solid var(--color-border)',
          }}
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
            <svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 8.5c0-3 2.4-5 6-5s6 2 6 5-2 5-5 5H6l-3 2.5V8.5Z" strokeLinejoin="round" />
            </svg>
            Discord
          </span>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4">
            <path d="M4.5 2.5h5v5" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M9.3 2.7 2.5 9.5" strokeLinecap="round" />
          </svg>
        </a>}

        <div style={{ border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)', padding: '0.6rem 0.75rem', display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
          <StatusRow label={n8nReachable ? 'n8n running' : 'n8n offline'} connected={n8nReachable} />
          <StatusRow label={backendConnected ? 'Backend connected' : 'Backend offline'} connected={backendConnected} />
        </div>
      </div>
    </div>
  )
}
