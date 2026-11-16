// Phase B B9-A: every page that lists offers records its own surface and position (ADR 0016), so
// the saved list, a search, the outside-criteria lane and Review are never read as the
// ranked main list.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import Jobs from '../Jobs'
import Review from '../Review'

const tracked: Array<{ surface: string; jobId: string; position: number }> = []
vi.mock('../../hooks/useVisibleExposure', () => ({
  useVisibleExposure: (surface: string) =>
    (jobId: string, position: number) => (el: Element | null) => {
      if (el) tracked.push({ surface, jobId, position })
    },
}))

const job = (id: string, title: string) => ({
  id, source: 'eures', title, company: 'ACME', location: 'Lyon', status: 'active', score: null,
  saved: false, hidden: false,
})
const posts: Array<{ url: string; body: Record<string, unknown> }> = []
vi.mock('axios', () => {
  const client = {
    get: (url: string) => {
      if (url === '/jobs/outside-criteria') return Promise.resolve({ data: [job('o1', 'Hors critères')] })
      if (url === '/jobs') return Promise.resolve({ data: [job('m1', 'Liste principale')] })
      if (url === '/decisions/review-queue') {
        return Promise.resolve({
          data: {
            total: 1, items: [{
              job_stored: false, has_snapshot: false,
              decision: {
                decision_id: 'd1', run_id: 'r1', job_id: 'j1', decided_at: '2026-10-01T00:00:00+00:00',
                outcome: 'rejected', deciding_stage: 'filter', strategy_version: 'sv-1', fit_score: null,
                decision_confidence: 0.5, confidence_calibrated: false, signals: {}, evidence: [], conflicts: [],
                stage_trace: [], exposure: null,
                summary: { supporting: 0, against: 0, unknown: 0, conflicts: 0, headline: 'rejected at filter' },
                job: { title: 'Technicien support', company: 'Company-1', source: 'src', location: 'Toulon' },
              },
            }],
          },
        })
      }
      return Promise.resolve({ data: null })
    },
    post: (url: string, body: Record<string, unknown>) => {
      posts.push({ url, body })
      return Promise.resolve({ data: { event_id: 'e1', job_id: 'j1' } })
    },
    put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

function wrap(node: React.ReactNode) {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient()}><ToastProvider>{node}</ToastProvider></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => { tracked.length = 0; posts.length = 0 })

describe('list surfaces (B9-A)', () => {
  it('records the main list and the outside-criteria lane under their own surface', async () => {
    wrap(<Jobs />)
    await screen.findByText('Liste principale')
    await waitFor(() => expect(tracked.some(t => t.jobId === 'o1')).toBe(true))
    expect(tracked).toContainEqual({ surface: 'jobs', jobId: 'm1', position: 0 })
    expect(tracked).toContainEqual({ surface: 'outside_criteria', jobId: 'o1', position: 0 })
  })

  it('records the saved list as "saved", not as the ranked list', async () => {
    wrap(<Jobs />)
    await screen.findByText('Liste principale')
    tracked.length = 0
    await userEvent.click(screen.getByLabelText('Saved offers only'))
    await waitFor(() => expect(tracked.some(t => t.surface === 'saved' && t.jobId === 'm1')).toBe(true))
    expect(tracked.filter(t => t.jobId === 'm1').every(t => t.surface === 'saved')).toBe(true)
  })

  it('records Review cards with their position and sends the answer with that provenance', async () => {
    wrap(<Review />)
    await screen.findByText('Technicien support')
    expect(tracked).toContainEqual({ surface: 'review', jobId: 'j1', position: 0 })
    await userEvent.click(screen.getByRole('button', { name: 'Not for me' }))
    await waitFor(() => expect(posts.length).toBe(1))
    expect(posts[0].url).toBe('/jobs/j1/feedback')
    expect(posts[0].body).toMatchObject({
      type: 'not_interested', decision_id: 'd1', source: 'ui', context: { surface: 'review', position: 0 },
    })
  })
})
