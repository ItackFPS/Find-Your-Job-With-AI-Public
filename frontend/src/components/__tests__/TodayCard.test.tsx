import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TodayCard } from '../TodayCard'
import { dashboard, job } from '../../test/apiFixtures'
import type { Dashboard, Job } from '../../api/client'

const listJobs = vi.fn()
const getSchedulerStatus = vi.fn()
vi.mock('../../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../../api/client')>(),
  listJobs: (params: unknown) => listJobs(params),
  getSchedulerStatus: () => getSchedulerStatus(),
}))

const show = (d: Dashboard) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><TodayCard dashboard={d} /></MemoryRouter>
  </QueryClientProvider>,
)

beforeEach(() => {
  listJobs.mockReset().mockResolvedValue([])
  getSchedulerStatus.mockReset().mockResolvedValue({
    paused: false, background: null,
    schedules: [{ id: 'pipeline_daily', next_run_at: '2026-11-09T08:00:00+00:00' }],
  })
})

describe('TodayCard', () => {
  it('asks only for strong matches, five at most', async () => {
    show(dashboard)
    await screen.findByText(/Next search/)
    expect(listJobs).toHaveBeenCalledWith({ min_tier: 'strong', limit: 5 })
  })

  it('links each top match to its explanation', async () => {
    const top: Job = { ...job, id: 'abc', title: 'Infirmier' }
    listJobs.mockResolvedValue([top])
    show(dashboard)
    expect(await screen.findByRole('link', { name: 'Infirmier' })).toHaveAttribute('href', '/jobs/abc')
  })

  it('points at a failed source', async () => {
    const stats = { ...dashboard.latest_run!.stats, by_source: { eures: { collected: 0, accepted: 0, errors: 1, status: 'failed' } } }
    show({ ...dashboard, latest_run: { ...dashboard.latest_run!, stats } })
    expect(await screen.findByRole('link', { name: /1 source/ })).toHaveAttribute('href', '/settings')
  })

  it('names a lost connection instead of counting failed sources', async () => {
    const down = { collected: 0, accepted: 0, errors: 1, status: 'failed', network_outage: true }
    const stats = { ...dashboard.latest_run!.stats, by_source: { eures: down, jobicy: down } }
    show({ ...dashboard, latest_run: { ...dashboard.latest_run!, stats } })
    expect(await screen.findByText(/internet connection/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /2 sources/ })).not.toBeInTheDocument()
  })

  it('says so before any search', async () => {
    show({ ...dashboard, latest_run: null })
    expect(await screen.findByText(/No search yet/)).toBeInTheDocument()
  })
})
