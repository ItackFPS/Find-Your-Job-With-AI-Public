// Issue #86: an expired offer says why; the user can report an offer as no longer
// available, or undo that report.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import type { Job } from '../../api/client'
import { AvailabilitySection } from '../JobDetail'

const posts: { url: string; body: unknown }[] = []
vi.mock('axios', () => {
  const client = {
    get: () => Promise.resolve({ data: null }),
    post: (url: string, body: unknown) => { posts.push({ url, body }); return Promise.resolve({ data: {} }) },
    put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

const base = {
  id: 'j1', source: 'eures', title: 'Développeur', status: 'active', valid_through: null,
  expired_at: null, expiry_reason: null,
} as unknown as Job

function renderSection(job: Job) {
  const queryClient = new QueryClient()
  return render(
    <QueryClientProvider client={queryClient}><ToastProvider><AvailabilitySection job={job} /></ToastProvider></QueryClientProvider>,
  )
}

beforeEach(() => { posts.length = 0 })

describe('AvailabilitySection', () => {
  it('lets the user report an open offer as no longer available', async () => {
    renderSection(base)
    await userEvent.click(screen.getByRole('button', { name: /no longer available/i }))
    expect(posts).toEqual([{ url: '/jobs/j1/availability', body: { available: false } }])
  })

  it('says why an offer expired, and offers no undo for an automatic expiry', () => {
    renderSection({ ...base, status: 'expired', expiry_reason: 'not_seen', expired_at: '2026-10-30T08:00:00Z' })
    expect(screen.getByRole('status')).toHaveTextContent(/no longer listed in recent searches/i)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('undoes the user report', async () => {
    renderSection({ ...base, status: 'expired', expiry_reason: 'user_report', expired_at: '2026-10-30T08:00:00Z' })
    expect(screen.getByRole('status')).toHaveTextContent(/you marked this offer/i)
    await userEvent.click(screen.getByRole('button', { name: /still available/i }))
    expect(posts).toEqual([{ url: '/jobs/j1/availability', body: { available: true } }])
  })

  it('shows the closing date of an open offer', () => {
    renderSection({ ...base, valid_through: '2026-11-30T22:59:59Z' })
    expect(screen.getByText(/open until/i)).toBeInTheDocument()
  })
})
