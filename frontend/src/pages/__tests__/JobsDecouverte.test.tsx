// Découverte (S4; ADR 0019): the latest run's random slots get their own section above the list,
// on the plain first page only.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import Jobs from '../Jobs'

const job = (id: string, title: string) => ({
  id, source: 'eures', title, company: 'ACME', location: 'Lyon', status: 'active', score: null,
  saved: false, hidden: false,
})
const gets: string[] = []
vi.mock('axios', () => {
  const client = {
    get: (url: string) => {
      gets.push(url)
      if (url === '/jobs/decouverte') return Promise.resolve({ data: [job('s1', 'Tirée au hasard')] })
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

beforeEach(() => { gets.length = 0 })

describe('Jobs page Découverte section', () => {
  it('shows the random slots apart from the main list', async () => {
    renderJobs()
    const section = await screen.findByTestId('decouverte')
    expect(within(section).getByText('Tirée au hasard')).toBeInTheDocument()
    expect(within(section).queryByText('Liste principale')).not.toBeInTheDocument()
    expect(await screen.findByText('Liste principale')).toBeInTheDocument()
    expect(gets).toContain('/jobs/decouverte')
  })

  it('hides the section while searching', async () => {
    renderJobs()
    await screen.findByTestId('decouverte')
    await userEvent.type(screen.getByPlaceholderText(/search jobs/i), 'rust')
    await vi.waitFor(() => expect(screen.queryByTestId('decouverte')).not.toBeInTheDocument(), { timeout: 2000 })
  })
})
