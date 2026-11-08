import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { profile as v1Profile, resolveFixture } from '../../test/apiFixtures'
import { ToastProvider } from '../../components'
import type { Profile as ProfileT } from '../../api/client'
import Onboarding from '../Onboarding'

const v2: ProfileT = {
  ...v1Profile, profile_version: 2, target_domains: [], experiences: [], education: [], review: null,
  targets: { levels: [], stretch: false, years_override: null },
}
const home = { name: 'Lyon', insee: '69123', lat: 45.76, lon: 4.83, department: '69', precision: 'commune' }

const net = {
  onboarding: { started: false, step: 0, completed: false } as Record<string, unknown>,
  profile: v2,
  puts: [] as { url: string; body: Record<string, unknown> }[],
  posts: [] as string[],
}

vi.mock('axios', () => {
  const path = (url: string) => url.replace(/^\/api/, '').split('?')[0]
  const client = {
    get: async (url: string) => {
      const p = path(url)
      if (p === '/settings/onboarding') return { data: net.onboarding }
      if (p === '/profile') return { data: net.profile }
      if (p === '/sources/overview') return { data: { sources: [] } }
      return { data: resolveFixture(p) ?? [] }
    },
    post: async (url: string) => { net.posts.push(path(url)); return { data: { run_id: 'r', status: 'started' } } },
    put: async (url: string, body: Record<string, unknown>) => {
      net.puts.push({ url: path(url), body })
      if (path(url) === '/settings/onboarding') {
        net.onboarding = { ...net.onboarding, started: true, ...body }
        return { data: net.onboarding }
      }
      return { data: body }
    },
    patch: async () => ({ data: {} }),
  }
  return { default: { create: () => client } }
})

function show() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><ToastProvider><Onboarding /></ToastProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}
const next = () => userEvent.click(screen.getByRole('button', { name: 'Next' }))
const onboardingPuts = () => net.puts.filter(p => p.url === '/settings/onboarding').map(p => p.body)

beforeEach(() => {
  net.onboarding = { started: false, step: 0, completed: false }
  net.profile = v2
  net.puts = []
  net.posts = []
})

describe('Onboarding', () => {
  it('states that any line of work can be searched and marks the setup as begun at once', async () => {
    show()
    expect(await screen.findByText(/any line of work can be searched/)).toBeInTheDocument()
    await waitFor(() => expect(onboardingPuts()).toContainEqual({ step: 0 }))
  })

  it('resumes at the stored step', async () => {
    net.onboarding = { started: true, step: 5, completed: false }
    show()
    expect(await screen.findByRole('button', { name: 'Start my first search' })).toBeInTheDocument()
  })

  it('will not leave the profile step without a field and a home town', async () => {
    net.onboarding = { started: true, step: 1, completed: false }
    show()
    expect(await screen.findByRole('button', { name: 'Next' })).toBeDisabled()
    expect(net.puts.some(p => p.url === '/profile')).toBe(false)
  })

  it('never offers the shipped example as the search', async () => {
    net.onboarding = { started: true, step: 1, completed: false, profile_saved: false }
    net.profile = { ...v2, target_domains: ['software_development'], geography: { ...v2.geography, home } as ProfileT['geography'] }
    show()
    expect(await screen.findByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /software_development/ })).not.toBeInTheDocument()
  })

  it('saves the profile only on Next', async () => {
    net.onboarding = { started: true, step: 1, completed: false, profile_saved: true }
    net.profile = { ...v2, target_domains: ['healthcare'], geography: { ...v2.geography, home } as ProfileT['geography'] }
    show()
    await screen.findByRole('button', { name: 'Next' })
    expect(net.puts.some(p => p.url === '/profile')).toBe(false)
    await next()
    await waitFor(() => expect(net.puts.some(p => p.url === '/profile')).toBe(true))
    await waitFor(() => expect(onboardingPuts()).toContainEqual({ step: 2 }))
  })

  it('skipping completes the setup and starts no search', async () => {
    show()
    await userEvent.click(await screen.findByRole('button', { name: 'Skip setup' }))
    await waitFor(() => expect(onboardingPuts()).toContainEqual({ completed: true }))
    expect(net.posts).not.toContain('/pipeline/run')
  })

  it('the last step can start the first search', async () => {
    net.onboarding = { started: true, step: 5, completed: false }
    show()
    await userEvent.click(await screen.findByRole('button', { name: 'Start my first search' }))
    await waitFor(() => expect(net.posts).toContain('/pipeline/run'))
    expect(onboardingPuts()).toContainEqual({ completed: true })
  })
})
