import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { profile as v1Profile, resolveFixture } from '../../test/apiFixtures'
import { ToastProvider } from '../../components'
import type { Profile as ProfileT } from '../../api/client'
import Profile from '../Profile'

const net: { profile: ProfileT; put: unknown[] } = { profile: v1Profile, put: [] }

vi.mock('axios', () => {
  const client = {
    get: async (url: string) => {
      const p = url.replace(/^\/api/, '').split('?')[0]
      return { data: p === '/profile' ? net.profile : resolveFixture(p) ?? [] }
    },
    post: async () => ({ data: {} }),
    put: async (_url: string, body: unknown) => { net.put.push(body); return { data: body } },
    patch: async () => ({ data: {} }),
  }
  return { default: { create: () => client } }
})

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><ToastProvider><Profile /></ToastProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

const v2: ProfileT = {
  ...v1Profile, profile_version: 2, experiences: [], education: [], review: null,
  targets: { levels: ['junior'], stretch: false, years_override: null },
}

beforeEach(() => { net.put = [] })

describe('Profile career targets', () => {
  it('edits target levels and stretch on a v2 profile, without the keyword lists', async () => {
    net.profile = v2
    renderPage()
    expect(await screen.findByText('Target career levels')).toBeInTheDocument()
    expect(screen.queryByText('Excluded seniority keywords')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Mid-level' }))
    await userEvent.click(screen.getByText('Show stretch roles'))
    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    const saved = net.put.at(-1) as ProfileT
    expect(saved.targets).toEqual({ levels: ['junior', 'mid'], stretch: true, years_override: null })
  })

  it('keeps the v1 keyword lists for a profile not yet converted', async () => {
    net.profile = v1Profile
    renderPage()
    expect(await screen.findByText('Excluded seniority keywords')).toBeInTheDocument()
    expect(screen.queryByText('Target career levels')).not.toBeInTheDocument()
  })
})
