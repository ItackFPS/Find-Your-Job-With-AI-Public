import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TodayCard } from '../TodayCard'
import { dashboard, job } from '../../test/apiFixtures'
import type { Dashboard, Job } from '../../api/client'

const listJobs = vi.fn()
const getSchedulerStatus = vi.fn()
const countJobs = vi.fn()
vi.mock('../../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../../api/client')>(),
  listJobs: (params: unknown) => listJobs(params),
  countJobs: (params: unknown) => countJobs(params),
  getSchedulerStatus: () => getSchedulerStatus(),
}))

const show = (d: Dashboard) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><TodayCard dashboard={d} /></MemoryRouter>
  </QueryClientProvider>,
)

beforeEach(() => {
  listJobs.mockReset().mockResolvedValue([])
  countJobs.mockReset().mockResolvedValue(7)
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
    const stats = { ...dashboard.latest_run!.stats, by_source: { eures: down, indeed: down } }
    show({ ...dashboard, latest_run: { ...dashboard.latest_run!, stats } })
    expect(await screen.findByText(/internet connection/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /2 sources/ })).not.toBeInTheDocument()
  })

  it('names the sources that failed (OQ-3)', async () => {
    const stats = { ...dashboard.latest_run!.stats, outcome: 'partial' as const, by_source: {
      eures: { collected: 0, accepted: 0, errors: 1, status: 'failed' },
      hellowork: { collected: 3, accepted: 1, errors: 1, status: 'partial' },
      jobicy: { collected: 4, accepted: 2, errors: 0, status: 'completed' } } }
    show({ ...dashboard, latest_run: { ...dashboard.latest_run!, stats } })
    expect(await screen.findByTestId('failed-sources')).toHaveTextContent('eures, hellowork')
  })

  it.each([
    ['no_successful_source', /previous search/],
    ['no_new_offers', /Nothing new/],
    ['first_run_empty', /first search found no offer/],
  ] as const)('says what a %s run means for the list', async (outcome, text) => {
    const stats = { ...dashboard.latest_run!.stats, outcome }
    show({ ...dashboard, latest_run: { ...dashboard.latest_run!, stats } })
    expect(await screen.findByTestId('run-outcome')).toHaveTextContent(text)
  })

  it('adds nothing for a successful run or one recorded before outcomes', async () => {
    show(dashboard)
    await screen.findByText(/Next search/)
    expect(screen.queryByTestId('run-outcome')).toBeNull()
  })

  it('announces the number the Jobs list shows by default (P-017)', async () => {
    show(dashboard)
    expect(await screen.findByText(/7 offers are relevant or better/)).toBeInTheDocument()
    expect(countJobs).toHaveBeenCalledWith({ min_tier: 'relevant' })
  })

  it('names a source that came back empty after returning offers, and the carried offers (issue #253)', async () => {
    const stats = { ...dashboard.latest_run!.stats, outcome: 'partial' as const, carried: 42,
      incomplete_sources: { indeed: 'suspect_empty' as const, eures: 'suspect_empty' as const } }
    show({ ...dashboard, latest_run: { ...dashboard.latest_run!, stats } })
    expect(await screen.findByTestId('suspect-sources')).toHaveTextContent('2 sources returned no offer although they found some before (eures, indeed)')
    expect(screen.getByTestId('carried')).toHaveTextContent('42 offers carried over from earlier searches')
  })

  it('says so before any search', async () => {
    show({ ...dashboard, latest_run: null })
    expect(await screen.findByText(/No search yet/)).toBeInTheDocument()
  })
})
