// Phase B B9-A: a read-only "what was recorded" view under Settings › Learning.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RecordedActivity } from '../RecordedActivity'

const gets: string[] = []
vi.mock('axios', () => {
  const client = {
    get: (url: string) => {
      gets.push(url)
      return Promise.resolve({
        data: {
          learning: { paused: false, reset_at: null },
          events: [
            { type: 'not_interested', signal_class: 'explicit', count: 2, counted: 1, last_at: '2026-11-14T09:00:00+00:00' },
            { type: 'viewed', signal_class: 'behavior', count: 5, counted: 5, last_at: null },
          ],
          event_surfaces: { review: 1, job_detail: 1 },
          exposures: { total: 3, by_surface: { jobs: 2, review: 1 }, by_source: { bundesagentur: 3 }, oldest_at: null,
                       compact_after_days: 30, keep_days: 180 },
          repeated_views: 'counted once per offer',
        },
      })
    },
    post: () => Promise.resolve({ data: {} }), put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

const wrap = (node: React.ReactNode) =>
  render(<QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>)

beforeEach(() => { gets.length = 0 })

describe('RecordedActivity', () => {
  it('fetches nothing until it is opened', () => {
    wrap(<RecordedActivity open={false} />)
    expect(gets).toEqual([])
  })

  it('shows each action with its counts, the lists, the sources and the retention', async () => {
    wrap(<RecordedActivity open />)
    expect(await screen.findByText('Not for me')).toBeInTheDocument()
    expect(gets).toEqual(['/privacy/recorded'])
    expect(screen.getByText('Offer opened')).toBeInTheDocument()
    expect(screen.getByText('3 offers shown, per list')).toBeInTheDocument()
    expect(screen.getByText('Offers shown, per source')).toBeInTheDocument()
    expect(screen.getByText(/summarised after 30 days and deleted after 180 days/)).toBeInTheDocument()
    expect(screen.getByText(/counts an offer's views only once/)).toBeInTheDocument()
    expect(screen.getByText(/Erase my activity removes it/)).toBeInTheDocument()
  })
})
