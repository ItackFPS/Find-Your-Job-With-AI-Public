// "Outside your criteria" (ADR 0019 v5 AD-2b; stage L5.3): weak-evidence offers get a collapsed
// section that names the criterion each one misses; it is absent when the lane is empty.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import Jobs from '../Jobs'

const job = (id: string, title: string, flags: string[] = []) => ({
  id, source: 'eures', title, company: 'ACME', location: 'Lyon', status: 'active', saved: false, hidden: false,
  score: flags.length ? {
    deterministic_score: 0.7, semantic_score: 0.5, final_score: 0.7, factors: {}, run_id: 'r', score_100: 70,
    tier: 'relevant', weak_penalty: 0.1,
    weak_flags: flags.map(code => ({ code, observed: null, expected: null, severity: 1 })),
  } : null,
})
let outside: unknown[] = []
vi.mock('axios', () => {
  const client = {
    get: (url: string) => {
      if (url === '/jobs/outside-criteria') return Promise.resolve({ data: outside })
      if (url === '/jobs/decouverte') return Promise.resolve({ data: [] })
      if (url === '/jobs') return Promise.resolve({ data: [job('m1', 'Liste principale')] })
      return Promise.resolve({ data: null })
    },
    post: () => Promise.resolve({ data: {} }),
    put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

function renderJobs() {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient()}><ToastProvider><Jobs /></ToastProvider></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => { outside = [] })

describe('Jobs page outside-your-criteria lane', () => {
  it('lists weak-evidence offers collapsed, naming the missed criteria', async () => {
    outside = [job('w1', 'Offre ancienne', ['published_age', 'seniority'])]
    renderJobs()
    const lane = await screen.findByTestId('outside-criteria')
    expect(lane.tagName).toBe('DETAILS')
    expect(lane).not.toHaveAttribute('open')
    expect(within(lane).getByText('Offre ancienne')).toBeInTheDocument()
    expect(within(lane).getByTestId('outside-criteria-misses').textContent)
      .toMatch(/older than your limit, a seniority word in the title/)
    expect(within(lane).queryByText('Liste principale')).not.toBeInTheDocument()
  })

  it('is absent when the lane is empty', async () => {
    renderJobs()
    expect(await screen.findByText('Liste principale')).toBeInTheDocument()
    expect(screen.queryByTestId('outside-criteria')).not.toBeInTheDocument()
  })
})
