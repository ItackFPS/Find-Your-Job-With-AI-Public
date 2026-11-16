// Issue #253: the Jobs page explains the list, pages by the real count and has a tracked view.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import Jobs from '../Jobs'

const job = (id: string, extra: Record<string, unknown> = {}) => ({
  id, source: 'eures', title: `Offre ${id}`, company: 'ACME', location: 'Lyon', status: 'active', score: null,
  saved: false, hidden: false, ...extra,
})
let listed: unknown[] = []
let count = 0
const calls: { url: string; params?: Record<string, unknown> }[] = []
vi.mock('axios', () => {
  const client = {
    get: (url: string, config?: { params?: Record<string, unknown> }) => {
      calls.push({ url, params: config?.params })
      if (url === '/jobs') return Promise.resolve({ data: listed })
      if (url === '/jobs/count') return Promise.resolve({ data: { count } })
      if (url === '/jobs/list-status') {
        return Promise.resolve({ data: {
          visible_run_id: 'r1', visible_run_completed_at: null, latest_run_id: 'r1', latest_run_status: 'completed',
          latest_run_outcome: 'partial', previous_list_kept: false, incomplete_sources: { eures: 'suspect_empty' },
          carried_by_source: { eures: 12 }, listed: 50, hidden_by_user: 0, decouverte: 0, outside_criteria: 0,
          source_off: {}, expired: {},
        } })
      }
      return Promise.resolve({ data: null })
    },
    post: () => Promise.resolve({ data: {} }),
    put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

const renderJobs = () => render(
  <MemoryRouter>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider><Jobs /></ToastProvider>
    </QueryClientProvider>
  </MemoryRouter>,
)

beforeEach(() => { calls.length = 0; listed = []; count = 0 })

describe('Jobs page list status (issue #253)', () => {
  it('names the incomplete source and the carried offers', async () => {
    listed = [job('a')]
    count = 1
    renderJobs()
    expect(await screen.findByTestId('list-status-incomplete')).toHaveTextContent('eures returned no offer')
    expect(screen.getByTestId('list-status-carried')).toHaveTextContent('12 offers in the list come from earlier searches')
  })

  it('stops paging at the real count, even when the last page is full', async () => {
    listed = Array.from({ length: 50 }, (_, i) => job(`j${i}`))
    count = 50
    renderJobs()
    expect(await screen.findByText('Offre j49')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Next/ })).not.toBeInTheDocument()   // was enabled, led to an empty page
  })

  it('shows the total next to the page range', async () => {
    listed = Array.from({ length: 50 }, (_, i) => job(`j${i}`))
    count = 120
    renderJobs()
    expect(await screen.findByText('1–50 / 120')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Next/ })).toBeEnabled()
  })

  it('lists tracked applications, with an expired offer marked', async () => {
    renderJobs()
    listed = [job('t1', { status: 'expired', expiry_reason: 'stale', application_status: 'applied', source_off: true })]
    count = 1
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Tracked applications only' }))
    expect(await screen.findByTestId('badge-expired')).toHaveTextContent('not seen for 30 days')
    expect(screen.getByTestId('badge-source-off')).toBeInTheDocument()
    expect(calls.some(c => c.url === '/jobs' && c.params?.tracked_only === true)).toBe(true)
    expect(screen.queryByTestId('list-status')).not.toBeInTheDocument()   // a personal view, not the list
  })

  it('says why the tracked view is empty', async () => {
    renderJobs()
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Tracked applications only' }))
    expect(await screen.findByText('No offer in the application tracker.')).toBeInTheDocument()
  })
})
