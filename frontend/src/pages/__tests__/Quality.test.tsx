import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as fixtures from '../../test/apiFixtures'
import { resolveFixture } from '../../test/apiFixtures'
import { ToastProvider } from '../../components'
import type { LabExperiment, QualityJob } from '../../api/client'
import { promotionCommand } from '../../utils'
import Quality, { SummaryView } from '../Quality'

type Handler = (url: string, body?: unknown) => Promise<{ data: unknown }>
const net: { get: Handler; post: Handler } = { get: async () => ({ data: null }), post: async () => ({ data: {} }) }
const fail = (status: number, detail?: unknown) =>
  Promise.reject(Object.assign(new Error('fail'), { response: { status, data: { detail } } }))

vi.mock('axios', () => {
  const client = {
    get: (url: string) => net.get(url),
    post: (url: string, body?: unknown) => net.post(url, body),
    put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

const path = (url: string) => url.replace(/^\/api/, '').split('?')[0]
function fixturesWith(overrides: Record<string, unknown> = {}): Handler {
  return async url => {
    const p = path(url)
    if (p in overrides) {
      const v = overrides[p]
      return typeof v === 'function' ? (v as () => Promise<{ data: unknown }>)() : { data: v }
    }
    const data = resolveFixture(p)
    return data === undefined ? fail(404) : { data }
  }
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><ToastProvider><Quality /></ToastProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

const running: QualityJob = { id: 'qj-9', kind: 'self_check', status: 'running', phase: 'running', options: {},
  created_at: '2026-10-24T10:00:00+00:00', finished_at: null, summary: null, error_code: null }

beforeEach(() => {
  net.get = fixturesWith()
  net.post = async () => ({ data: {} })
})

describe('Quality page', () => {
  it('shows the health summary as plain sentences, the history and the recent runs', async () => {
    renderPage()
    expect(await screen.findAllByText('Matching is safe: 0 suitable offers rejected on the benchmark.')).toHaveLength(2)
    expect(screen.getAllByText('Of the offers it kept, 88% were really suitable (3 unsuitable offers kept).')[0]).toBeInTheDocument()
    expect(screen.getAllByText(/Ranking quality is not measured yet/)[0]).toBeInTheDocument()
    expect(screen.getAllByText(/Only 22 suitable offers in this test set/)[0]).toBeInTheDocument()
    expect(screen.getByText(/You gave feedback on 5 of 50 decided offers \(10%\); 1 answer disagrees/)).toBeInTheDocument()
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    // history: a failed job explains itself from its code; a completed one shows its numbers
    expect(await screen.findByText('The check could not run. Details are in the backend log.')).toBeInTheDocument()
    expect(screen.getByText('0 suitable rejected · precision 88% · recall 100%')).toBeInTheDocument()
    // runs
    expect(await screen.findByText('1240 found, 640 kept')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'See the full funnel' })).toHaveAttribute('href', '/pipeline')
  })

  it('offers to run a first check when there is none, and empty history states', async () => {
    net.get = fixturesWith({ '/quality/health': { self_check: null, evaluation: null },
      '/quality/jobs': { active_id: null, jobs: [] }, '/pipeline/runs': [] })
    renderPage()
    expect(await screen.findByText('No health check has been run yet. It takes a few seconds.')).toBeInTheDocument()
    expect(await screen.findByText('No check has been run from here yet.')).toBeInTheDocument()
    expect(await screen.findByText('No search has run yet.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Run health check' })).toHaveLength(2)
  })

  it('shows an error with a working retry when the health summary fails', async () => {
    let calls = 0
    net.get = fixturesWith({ '/quality/health': () => (++calls === 1 ? fail(500) : Promise.resolve({ data: fixtures.qualityHealth })) })
    renderPage()
    expect(await screen.findByText('The health summary could not be loaded.')).toBeInTheDocument()
    await userEvent.click(within(screen.getByText('The health summary could not be loaded.').closest('[role="alert"]') as HTMLElement)
      .getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Healthy')).toBeInTheDocument()
  })

  it('starts a health check, shows live progress, then says it is done', async () => {
    let current: QualityJob = running
    net.get = fixturesWith({ '/quality/jobs/qj-9': () => Promise.resolve({ data: current }) })
    const posts: [string, unknown][] = []
    net.post = async (url, body) => { posts.push([path(url), body]); return { data: running } }
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Run evaluation' }))
    expect(posts).toEqual([['/quality/jobs', { kind: 'evaluation' }]])
    expect(await screen.findByText('Health check running…')).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Progress' })).toBeInTheDocument()
    expect(screen.getByText('Replaying the benchmark').closest('li')).toHaveAttribute('aria-current', 'step')
    current = { ...running, status: 'completed', phase: null, summary: fixtures.qualitySummary }
    expect(await screen.findByText('Health check finished.', {}, { timeout: 3000 })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run health check' })).toBeEnabled()
  })

  it('cancels the running job', async () => {
    let current: QualityJob = running
    net.get = fixturesWith({ '/quality/jobs': { active_id: 'qj-9', jobs: [running] },
      '/quality/jobs/qj-9': () => Promise.resolve({ data: current }) })
    const posts: string[] = []
    net.post = async url => { posts.push(path(url)); current = { ...running, status: 'cancelled', phase: null }; return { data: { cancelling: true } } }
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(posts).toEqual(['/quality/jobs/qj-9/cancel'])
    expect(await screen.findByRole('button', { name: 'Run health check' }, { timeout: 3000 })).toBeEnabled()
  })

  it('says so when another check is already running', async () => {
    net.post = () => fail(409, { code: 'busy', active_id: 'qj-9' })
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Run health check' }))
    expect(await screen.findByText('Another check is already running: wait for it to finish or cancel it.')).toBeInTheDocument()
  })

  it('shows the CLI promotion command with a copy button, never a promote action', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const eligible: LabExperiment = { ...fixtures.labExperiments.experiments[0], experiment_id: 'exp-7', eligible_for_approval: true,
      promotion_evidence: 'exploratory_only', hypothesis: '<b>exclude</b> <a href="x">defense</a>' }
    net.get = fixturesWith({ '/lab/experiments': { experiments: [eligible] } })
    renderPage()
    const cmd = 'python -m src.lab approve exp-7 --dataset data/benchmarks/real-v1 --by "<your name>" --reason "<why>"'
    expect(await screen.findByText(cmd)).toBeInTheDocument()
    // lab text is plain text: the markup shows literally and makes no link
    expect(screen.getByText('<b>exclude</b> <a href="x">defense</a>')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'defense' })).toBeNull()
    expect(screen.getByText(/rests on exploratory evidence only/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve|promote/i })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Copy command' }))
    expect(writeText).toHaveBeenCalledWith(cmd)
    expect(await screen.findByText('Command copied.')).toBeInTheDocument()
  })

  it('shows the full lab only behind the Advanced switch', async () => {
    renderPage()
    expect(screen.queryByRole('heading', { level: 2, name: 'Lab' })).toBeNull()
    await userEvent.click(await screen.findByRole('switch'))
    expect(await screen.findByRole('heading', { level: 2, name: 'Lab' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('exclude defense')).toBeInTheDocument())
  })
})

describe('promotionCommand', () => {
  const ph = { experiment: 'experiment-id', dataset: 'dataset-folder', name: 'your name', reason: 'why' }
  it('only puts plain identifiers from the lab into the command', () => {
    expect(promotionCommand({ experiment_id: 'exp-1', dataset_id: 'real-v1' }, ph))
      .toBe('python -m src.lab approve exp-1 --dataset data/benchmarks/real-v1 --by "<your name>" --reason "<why>"')
    for (const bad of ['x; rm -rf ~', 'a && b', '$(evil)', '`x`', '../../etc', '-flag', 'a b', '']) {
      expect(promotionCommand({ experiment_id: bad, dataset_id: 'real-v1' }, ph)).toContain('approve <experiment-id> --dataset <dataset-folder>')
      expect(promotionCommand({ experiment_id: 'exp-1', dataset_id: bad }, ph)).toContain('approve <experiment-id> --dataset <dataset-folder>')
    }
    expect(promotionCommand(null, ph)).toContain('approve <experiment-id>')
  })
})

describe('SummaryView', () => {
  it('words an unsafe result and ignores unknown codes', () => {
    const d = { ...fixtures.qualitySummary.datasets[0], unsafe_rejections: 2, flags: ['synthetic_benchmark', 'made_up'] as never }
    render(<SummaryView s={{ ...fixtures.qualitySummary, status: 'safety_violation', datasets: [d], feedback: { status: 'unavailable' } }} />)
    expect(screen.getByText('2 suitable offers were rejected on the benchmark.')).toBeInTheDocument()
    expect(screen.getByText('Unsafe')).toBeInTheDocument()
    expect(screen.getByText('Feedback coverage is not available yet: no decisions are recorded.')).toBeInTheDocument()
    expect(screen.queryByText(/made_up/)).toBeNull()
  })
})
