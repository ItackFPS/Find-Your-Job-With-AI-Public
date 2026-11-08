import { beforeAll, describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import axe from 'axe-core'
import { ToastProvider } from '../components'
import App from '../App'
import { resolveFixture } from './apiFixtures'

// Every page, with no serious accessibility violation. jsdom has no layout, so colour contrast
// cannot be measured here: that stays a manual check on the built app.
vi.mock('axios', () => {
  const answer = async (url: string) => {
    const p = url.replace(/^\/api/, '').split('?')[0]
    if (p === '/settings/onboarding') return { data: { started: true, step: 0, completed: true } }
    if (p === '/sources/overview') return { data: { sources: [] } }
    if (p === '/settings/data') return { data: {
      database_bytes: 3.5 * 2 ** 30, offers: { active: 1200, expired: 340 },
      removable_expired: { '30': 300, '90': 120, '180': 40 }, backups: [], cleanup: { state: 'idle' },
    } }
    if (p === '/settings/background') return { data: { enabled: true, daily_time: '08:00' } }
    if (p === '/scheduler/status') return { data: { schedules: [], paused: false, background: null } }
    return { data: resolveFixture(p) ?? [] }
  }
  const client = { get: answer, post: answer, put: answer, patch: answer, delete: answer }
  return { default: { create: () => client } }
})

class FakeEventSource {
  onopen = null
  onmessage = null
  onerror = null
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

beforeAll(() => { vi.stubGlobal('EventSource', FakeEventSource) })

const PAGES = ['/', '/jobs', '/review', '/profile', '/stats', '/skills', '/quality', '/pipeline', '/settings']
const SERIOUS = new Set(['serious', 'critical'])

describe('accessibility', () => {
  it.each(PAGES)('%s has no serious violation', async path => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={[path]}><ToastProvider><App /></ToastProvider></MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(container.querySelector('main')?.textContent).not.toBe(''))
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } })
    const found = result.violations
      .filter(v => SERIOUS.has(v.impact ?? ''))
      .map(v => `${v.id}: ${v.nodes.map(n => n.html.slice(0, 100)).join(' | ')}`)
    expect(found).toEqual([])
  }, 30000)
})
