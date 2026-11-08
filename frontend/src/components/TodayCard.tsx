import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { getSchedulerStatus, listJobs, type Dashboard } from '../api/client'
import { formatDateTime } from '../utils'
import { Card } from './Card'

const link = { color: 'var(--color-accent)' }
const line = { margin: 0 }

/** What a person opening the app wants: when it searched, what it found, whether anything broke. */
export function TodayCard({ dashboard }: { dashboard: Dashboard }) {
  const { t } = useTranslation()
  const run = dashboard.latest_run
  const matches = useQuery({
    queryKey: ['today-matches', run?.id ?? null],
    queryFn: () => listJobs({ min_tier: 'strong', limit: 5 }),
  })
  const scheduler = useQuery({ queryKey: ['scheduler-status'], queryFn: getSchedulerStatus, retry: false })

  const nextRun = scheduler.data?.schedules.find(s => s.id === 'pipeline_daily')?.next_run_at ?? null
  const sources = Object.values(run?.stats.by_source ?? {})
  const problems = sources.filter(s => s.status === 'failed').length
  const offline = sources.some(s => s.network_outage)

  return (
    <Card title={t('dashboard.today.title')} style={{ marginBottom: 'var(--space-4)' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
        <p style={line}>
          {run?.completed_at
            ? t('dashboard.today.lastRun', { date: formatDateTime(run.completed_at), count: run.stats.personalized ?? 0 })
            : t('dashboard.today.noRun')}
        </p>
        {nextRun && <p style={{ ...line, color: 'var(--color-muted)' }}>{t('dashboard.today.nextRun', { date: formatDateTime(nextRun) })}</p>}
        {offline && <p style={{ ...line, color: 'var(--color-warning)' }}>{t('dashboard.today.offline')}</p>}
        {problems > 0 && !offline && (
          <p style={line}>
            <Link to="/settings" style={{ color: 'var(--color-warning)' }}>{t('dashboard.today.problems', { count: problems })}</Link>
          </p>
        )}
        {matches.data && matches.data.length > 0 && (
          <>
            <h3 style={{ margin: 'var(--space-2) 0 0', fontSize: 'var(--font-size-base)' }}>{t('dashboard.today.top')}</h3>
            <ul style={{ margin: 0, paddingLeft: '1.25rem' }}>
              {matches.data.map(job => (
                <li key={job.id}>
                  <Link to={`/jobs/${job.id}`} style={link}>{job.title ?? t('dashboard.today.untitled')}</Link>
                  {job.company && <span style={{ color: 'var(--color-muted)' }}> · {job.company}</span>}
                </li>
              ))}
            </ul>
          </>
        )}
        {matches.data?.length === 0 && run && <p style={{ ...line, color: 'var(--color-muted)' }}>{t('dashboard.today.noMatch')}</p>}
      </div>
    </Card>
  )
}
