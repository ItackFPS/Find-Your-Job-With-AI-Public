import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import type { SchedulerStatus, SourceOverview } from '../../api/client'
import Settings from '../Settings'

type Call = { method: string; url: string; body?: unknown }
const calls: Call[] = []
let routes: Record<string, unknown> = {}

const path = (url: string) => url.replace(/^\/api/, '').split('?')[0]
const respond = (method: string) => async (url: string, body?: unknown) => {
  calls.push({ method, url: path(url), body })
  const key = `${method} ${path(url)}`
  if (key in routes) return { data: routes[key] }
  return Promise.reject(Object.assign(new Error('fail'), { response: { status: 404 } }))
}

vi.mock('axios', () => {
  const client = {
    get: (url: string) => respond('GET')(url),
    post: (url: string, body?: unknown) => respond('POST')(url, body),
    put: (url: string, body?: unknown) => respond('PUT')(url, body),
    patch: (url: string, body?: unknown) => respond('PATCH')(url, body),
  }
  return { default: { create: () => client } }
})

const scheduler: SchedulerStatus = {
  paused: false,
  background: { enabled: true, daily_time: '08:00' },
  schedules: [
    {
      id: 'pipeline_daily', source_id: 'pipeline', label: 'Daily search', enabled: true,
      startup_policy: 'when_profile_saved', daily: true, due: false, next_run_at: '2026-10-31T07:00:00+00:00',
      is_running: false,
      last_execution: { id: 'e1', trigger: 'startup', status: 'completed', started_at: '2026-10-30T07:00:00+00:00', finished_at: '2026-10-30T07:04:00+00:00', error: null },
    },
    {
      id: 'linkedin_daily', source_id: 'linkedin', label: 'LinkedIn daily scan', enabled: true,
      startup_policy: 'when_opted_in', daily: false, due: false, next_run_at: null, is_running: false,
      last_execution: { id: 'e2', trigger: 'manual', status: 'failed', started_at: '2026-10-29T10:00:00+00:00', finished_at: '2026-10-29T10:01:00+00:00', error: 'linkedin did not complete (run_ref=x)' },
    },
  ],
}

const sources: SourceOverview[] = [
  {
    name: 'france-travail-web', access_class: 'public_page', available: true, collected: true, choice: null,
    last_run: { started_at: '2026-10-30T07:00:00+00:00', status: 'completed', reason: null, collected: 40, accepted: 12, errors: 0 },
    breaker: null,
  },
  {
    name: 'remoteok', access_class: 'official', available: true, collected: true, choice: null,
    last_run: { started_at: '2026-10-30T07:00:00+00:00', status: 'failed', reason: 'The site did not answer', collected: 0, accepted: 0, errors: 1 },
    breaker: { source: 'remoteok', consecutive_failures: 3, paused: true, paused_until: '2026-10-30T12:00:00+00:00', last_failure_reason: 'timeout' },
  },
  {
    name: 'indeed', access_class: 'unofficial', available: true, collected: false,
    choice: { enabled: false, chosen_at: null }, last_run: null, breaker: null,
  },
]

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><ToastProvider><Settings /></ToastProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  calls.length = 0
  routes = {
    'GET /settings/background': { enabled: true, daily_time: '08:00' },
    'GET /scheduler/status': scheduler,
    'GET /sources/overview': { sources },
    'PUT /settings/background': { enabled: false, daily_time: '08:00' },
    'POST /scheduler/run/pipeline_daily': { exec_id: 'e3', status: 'started' },
    'PUT /sources/indeed/choice': { enabled: true, chosen_at: '2026-10-30T09:00:00+00:00' },
    'POST /sources/remoteok/breaker/reset': { source: 'remoteok', consecutive_failures: 0, paused: false, paused_until: null, last_failure_reason: null },
  }
})

describe('Settings › Sources and background', () => {
  it('turns background collection off', async () => {
    renderPage()
    const toggle = await screen.findByRole('switch', { name: /collect in the background/i })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    await userEvent.click(toggle)
    expect(calls).toContainEqual({ method: 'PUT', url: '/settings/background', body: { enabled: false } })
  })

  it('saves the daily time', async () => {
    renderPage()
    const input = await screen.findByLabelText(/daily time/i)
    await userEvent.clear(input)
    await userEvent.type(input, '07:30')
    await userEvent.click(screen.getByRole('button', { name: /save time/i }))
    expect(calls).toContainEqual({ method: 'PUT', url: '/settings/background', body: { daily_time: '07:30' } })
  })

  it('shows each daily run in plain words and starts one now', async () => {
    renderPage()
    const search = await screen.findByRole('listitem', { name: 'Daily search' })
    expect(within(search).getByText(/next run:/i)).toBeInTheDocument()
    const linkedin = screen.getByRole('listitem', { name: 'LinkedIn scan' })
    expect(within(linkedin).getByText(/turn the source on below/i)).toBeInTheDocument()
    expect(within(linkedin).getByText(/it did not finish/i)).toBeInTheDocument()
    expect(within(linkedin).queryByText(/run_ref/)).not.toBeInTheDocument()

    await userEvent.click(within(search).getByRole('button', { name: /run now/i }))
    expect(calls).toContainEqual({ method: 'POST', url: '/scheduler/run/pipeline_daily', body: undefined })
  })

  it('groups sources by kind with their latest result', async () => {
    renderPage()
    const official = await screen.findByRole('region', { name: /official and public sources/i })
    const ftw = within(official).getByRole('listitem', { name: 'France Travail (website)' })
    expect(within(ftw).getByText('Public pages')).toBeInTheDocument()
    expect(within(ftw).getByText('40 found, 12 kept')).toBeInTheDocument()
    const unofficial = screen.getByRole('region', { name: /unofficial sources/i })
    expect(within(unofficial).getByRole('listitem', { name: 'Indeed' })).toBeInTheDocument()
    expect(within(official).queryByRole('listitem', { name: 'Indeed' })).not.toBeInTheDocument()
  })

  it('records the choice for an unofficial source after showing its risk', async () => {
    renderPage()
    const indeed = await screen.findByRole('listitem', { name: 'Indeed' })
    expect(within(indeed).getByText(/does not allow automated searches/i)).toBeInTheDocument()
    expect(within(indeed).getByText(/never turned on/i)).toBeInTheDocument()
    await userEvent.click(within(indeed).getByRole('switch', { name: /collect from indeed/i }))
    expect(calls).toContainEqual({ method: 'PUT', url: '/sources/indeed/choice', body: { enabled: true } })
  })

  it('lets the user retry a paused source', async () => {
    renderPage()
    const remoteok = await screen.findByRole('listitem', { name: 'Remote OK' })
    expect(within(remoteok).getByText(/paused after 3 failures in a row/i)).toBeInTheDocument()
    await userEvent.click(within(remoteok).getByRole('button', { name: /try again now/i }))
    expect(calls).toContainEqual({ method: 'POST', url: '/sources/remoteok/breaker/reset', body: undefined })
  })

  it('reports a section that cannot load', async () => {
    delete routes['GET /sources/overview']
    renderPage()
    expect(await screen.findByText(/could not load this section/i)).toBeInTheDocument()
  })
})
