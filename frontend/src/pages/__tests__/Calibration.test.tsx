import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToastProvider } from '../../components'
import type { CalibrationItem, CalibrationOverview } from '../../api/client'
import CalibrationCard from '../Calibration'

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
