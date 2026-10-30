import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import type { CalibrationItem, CalibrationOverview, PersonalSummary } from '../../api/client'
import CalibrationCard, { PersonalResult } from '../Calibration'

type Handler = (url: string, body?: unknown) => Promise<{ data: unknown }>
const net: { get: Handler; post: Handler } = { get: async () => ({ data: null }), post: async () => ({ data: {} }) }

vi.mock('axios', () => {
  const client = {
    get: (url: string) => net.get(url),
    post: (url: string, body?: unknown) => net.post(url, body),
    put: () => Promise.resolve({ data: {} }),
    patch: () => Promise.resolve({ data: {} }),
  }
  return { default: { create: () => client } }
})

const dataset = { dataset_id: 'real-v2', version: '1', total: 310, labelled: 0, pending: 0, target: 100,
  by_answer: { relevant: 0, not_relevant: 0, not_sure: 0 }, provenance: {} }
const overview: CalibrationOverview = { datasets: [dataset], exports_found: [], reasons: ['location', 'other'], max_session: 30 }
const item = (id: string): CalibrationItem => ({ job_id: id, title: `Data analyst ${id}`, company: 'Acme', location: 'Lyon',
  remote: false, source: 'france_travail', description: 'Analyse de données', description_truncated: false })

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><ToastProvider><CalibrationCard /></ToastProvider></QueryClientProvider>)
}

describe('CalibrationCard', () => {
  let posts: { url: string; body: unknown }[]
  beforeEach(() => {
    posts = []
    net.get = async url => (url.includes('/next') ? { data: { items: [item('a1'), item('b2')] } } : { data: overview })
    net.post = async (url, body) => {
      posts.push({ url, body })
      return { data: url.endsWith('/save') ? { ...dataset, labelled: 2, version: '2', changed: 2 } : { ...dataset, pending: posts.length } }
    }
  })

  it('shows progress towards the first 100 labels', async () => {
    renderCard()
    expect(await screen.findByText('0 of 100 offers labelled (real-v2)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Label 10 offers' })).toBeInTheDocument()
  })

  it('labels offers blind, one by one, then saves the session', async () => {
    const user = userEvent.setup()
    renderCard()
    await user.click(await screen.findByRole('button', { name: 'Label 10 offers' }))
    expect(await screen.findByText('Data analyst a1')).toBeInTheDocument()
    expect(screen.getByText('Offer 1 of 2')).toBeInTheDocument()
    expect(screen.queryByText(/score|tier|strong/i)).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Location' }))
    await user.click(screen.getByRole('button', { name: 'Not relevant' }))
    expect(await screen.findByText('Data analyst b2')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Relevant' }))
    await waitFor(() => expect(posts.map(p => p.url)).toEqual([
      '/quality/calibration/real-v2/answers', '/quality/calibration/real-v2/answers', '/quality/calibration/real-v2/save']))
    expect(posts[0].body).toEqual({ job_id: 'a1', answer: 'not_relevant', reason: 'location' })
    expect(posts[1].body).toEqual({ job_id: 'b2', answer: 'relevant', reason: null })
    expect(await screen.findByText('2 answers saved (version 2)')).toBeInTheDocument()
  })

  it('offers exports found in the data folder', async () => {
    net.get = async () => ({ data: { ...overview, datasets: [], exports_found: [{ source: 'D:/x/real-v2', dataset_id: 'real-v2', total: 310 }] } })
    const user = userEvent.setup()
    renderCard()
    expect(await screen.findByText('Set “real-v2” found (310 offers)')).toBeInTheDocument()
    await user.click(screen.getAllByRole('button', { name: 'Import' })[0])
    await waitFor(() => expect(posts).toEqual([{ url: '/quality/calibration/import', body: { source: 'D:/x/real-v2' } }]))
  })
})

const summary: PersonalSummary = {
  kind: 'personal', created_at: null, report_file: null, warnings: 0, flags: ['few_suitable_labels'],
  dataset: { id: 'real-v2@heldout', version: '4', split: 'heldout', labels: 31, accept_labels: 6 },
  ranking: { auc: 0.71, auc_ci95: [0.52, 0.86], n_accept: 5, n_reject: 20,
    precision_at_strong: { value: 0.5, ci95: [0.19, 0.81], n: 8 }, recall_at_20: { value: 0.5, ci95: [0.19, 0.81], in_top: 3, n_accept: 6 } },
  representative_auc: null,
  missed: { labelled: 9, accept: 1, rate_ci95: [0.02, 0.43], estimate: [400, 2232, 8636], pool: 20085 },
  unsafe_rejections: 2, unsafe_freshness_only: 1,
  false_positives: { total: 4, by_category: { location: 3, domain: 1 }, by_source: { 'france-travail-web': 4 } },
}

describe('PersonalResult', () => {
  it('states ranking, misses and false positives with their intervals and the evidence caveat', () => {
    render(<PersonalResult s={summary} />)
    expect(screen.getByText(/31 held-out labels of “real-v2@heldout” \(version 4\), 6 of them relevant/)).toBeInTheDocument()
    expect(screen.getByText(/ranked above a not-relevant one 71 % of the time \(likely between 52 % and 86 %\)/)).toBeInTheDocument()
    expect(screen.getByText(/Of the 8 offers rated Strong or better, 50 %/)).toBeInTheDocument()
    expect(screen.getByText(/about 2232 suitable offers missed out of 20085/)).toBeInTheDocument()
    expect(screen.getByText(/2 relevant offers were rejected, 1 of them only because it was old/)).toBeInTheDocument()
    expect(screen.getByText(/By your reason: Location 3, Field 1\./)).toBeInTheDocument()
    expect(screen.getByText(/insufficient evidence/)).toBeInTheDocument()
  })
})

describe('measuring a set', () => {
  it('starts a personal measure on the held-out labels', async () => {
    const posts: { url: string; body: unknown }[] = []
    net.get = async () => ({ data: { ...overview, datasets: [{ ...dataset, labelled: 40 }] } })
    net.post = async (url, body) => { posts.push({ url, body }); return { data: { id: 'qj', kind: 'personal', status: 'running' } } }
    const user = userEvent.setup()
    renderCard()
    await user.click(await screen.findByRole('button', { name: 'Measure on held-out labels' }))
    await waitFor(() => expect(posts).toEqual([{ url: '/quality/jobs', body: { kind: 'personal', options: { dataset_id: 'real-v2', split: 'heldout' } } }]))
  })
})
