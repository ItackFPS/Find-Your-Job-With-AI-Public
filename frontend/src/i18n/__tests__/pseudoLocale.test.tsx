// F7.1 acceptance: under the pseudo-locale every UI string is accented ([!! Ðåššĥƀöåŕð !!]), so
// any plain ASCII word left on screen is either data from the API or a string that was never
// translated. Each page is rendered with data, with empty data and with failing requests, so
// loaded, empty and error states are all checked.
import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as fixtures from '../../test/apiFixtures'
import { resolveFixture } from '../../test/apiFixtures'
import i18n, { PSEUDO_LOCALE } from '..'
import App from '../../App'
import { ToastProvider } from '../../components'

type Mode = 'data' | 'empty' | 'error'
const state: { mode: Mode } = { mode: 'data' }

const EMPTY: Record<string, unknown> = {
  '/dashboard': { latest_run: null, total_jobs_in_db: 0, is_running: false, current_run_id: null },
  '/jobs': [],
  '/pipeline/runs': [],
  '/jobs/stats': { total_jobs: 0, skills: [], tools: [], score_tiers: [], rejections: [], title_categories: [] },
  '/decisions/review-queue': { total: 0, limit: 50, offset: 0, items: [] },
  '/skills/graph': { ...fixtures.skillGraph, nodes: [], edges: [], gaps: [] },
  '/lab/runs': { total: 0, unreadable: 0, runs: [] },
  '/lab/experiments': { experiments: [] },
  '/lab/memory': { items: [] },
  '/quality/health': { self_check: null, evaluation: null },
  '/quality/jobs': { active_id: null, jobs: [] },
  '/linkedin/status': { ...fixtures.linkedInStatus, last_scan: null },
  '/linkedin/scans': [],
  '/hellowork/status': { ...fixtures.helloWorkStatus, last_scan: null },
  '/hellowork/scans': [],
}

function respond(url: string): Promise<{ data: unknown }> {
  const path = url.replace(/^\/api/, '').split('?')[0]
  if (state.mode === 'error' && path !== '/health') return Promise.reject(Object.assign(new Error('Network Error'), { response: { status: 500 } }))
  if (state.mode === 'empty' && path in EMPTY) return Promise.resolve({ data: EMPTY[path] })
  const data = resolveFixture(path)
  return data === undefined
    ? Promise.reject(Object.assign(new Error('Not found'), { response: { status: 404 } }))
    : Promise.resolve({ data })
}

vi.mock('axios', () => {
  const client = {
    get: (url: string) => respond(url),
    post: () => Promise.resolve({ data: {} }),
    put: () => Promise.resolve({ data: fixtures.profile }),
    patch: () => Promise.resolve({ data: fixtures.job }),
  }
  return { default: { create: () => client } }
})

class FakeEventSource {
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

// Words that may stay in plain ASCII: anything in the fixture data, product and source
// names, and technical tokens shown verbatim.
const DATA_WORDS = new Set((JSON.stringify(fixtures).match(/[A-Za-z]{2,}/g) ?? []).map(w => w.toLowerCase()))
const ALLOWED = new Set([
  'find', 'your', 'job', 'with', 'ai', 'linkedin', 'hellowork', 'esco', 'france', 'travail', 'indeed', 'remoteok', 'jobicy',
  'we', 'work', 'remotely', 'eures', 'aa', 'ms', 'pts', 'vs', 'xa',
].map(w => w.toLowerCase()))

function isExempt(el: Element | null): boolean {
  return !!el?.closest('code, kbd, script, style, [translate="no"]')
}

function untranslated(root: HTMLElement): string[] {
  const texts: string[] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!isExempt(node.parentElement) && node.textContent?.trim()) texts.push(node.textContent)
  }
  root.querySelectorAll('[placeholder], [title], [aria-label]').forEach(el => {
    if (isExempt(el)) return
    for (const attr of ['placeholder', 'title', 'aria-label']) {
      const v = el.getAttribute(attr)
      if (v) texts.push(`${attr}="${v}"`.replace(/^[\w-]+="/, '').replace(/"$/, ''))
    }
  })
  return texts.filter(text =>
    (text.match(/[A-Za-z]{2,}/g) ?? []).some(w => !DATA_WORDS.has(w.toLowerCase()) && !ALLOWED.has(w.toLowerCase())),
  )
}

const PAGES = ['/', '/jobs', '/jobs/job-1', '/profile', '/stats', '/review', '/skills', '/lab', '/quality', '/pipeline', '/design']

async function renderPage(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <ToastProvider><App /></ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await waitFor(() => expect(queryClient.isFetching()).toBe(0))
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  // Open collapsed history rows so their detail panels are checked too.
  for (const toggle of Array.from(view.container.querySelectorAll('span')).filter(s => s.textContent === '▼')) {
    act(() => (toggle.parentElement as HTMLElement).click())
  }
  return view
}

describe('the untranslated-string detector', () => {
  it('flags plain English and ignores data, code and translate="no"', () => {
    const { container } = render(
      <div>
        <p>Hello world</p><p>[!! Ĥéééļļöö !!]</p><p>Acme</p><code>logs</code><span translate="no">senior</span>
        <input placeholder="Type here" />
      </div>,
    )
    expect(untranslated(container)).toEqual(['Hello world', 'Type here'])
  })
})

describe('pseudo-locale shows no untranslated UI string', () => {
  beforeAll(async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    await i18n.changeLanguage(PSEUDO_LOCALE)
  })
  afterAll(async () => {
    vi.unstubAllGlobals()
    await i18n.changeLanguage('en')
  })

  for (const mode of ['data', 'empty', 'error'] as Mode[]) {
    it.each(PAGES)(`%s (${mode})`, async path => {
      state.mode = mode
      const { container } = await renderPage(path)
      expect(container.textContent).toContain('[!!')
      expect(untranslated(container)).toEqual([])
    })
  }
})
