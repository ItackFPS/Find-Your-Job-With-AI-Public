import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ModelDownloadCard } from '../ModelDownloadCard'

const getEmbeddingModel = vi.fn()
const startEmbeddingModelDownload = vi.fn()
vi.mock('../../../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../../../api/client')>(),
  getEmbeddingModel: () => getEmbeddingModel(),
  startEmbeddingModelDownload: () => startEmbeddingModelDownload(),
}))

const MB = 1024 * 1024
const status = (over: object) => ({ state: 'missing', total_bytes: 457 * MB, downloaded_bytes: 0, error: null, ...over })

function show() {
  return render(<QueryClientProvider client={new QueryClient()}><ModelDownloadCard /></QueryClientProvider>)
}

describe('ModelDownloadCard', () => {
  beforeEach(() => { getEmbeddingModel.mockReset(); startEmbeddingModelDownload.mockReset() })

  it('states the size before asking and starts on request', async () => {
    getEmbeddingModel.mockResolvedValue(status({}))
    startEmbeddingModelDownload.mockResolvedValue(status({ state: 'downloading' }))
    show()
    expect(await screen.findByText(/457 MB/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Download the model' }))
    await waitFor(() => expect(startEmbeddingModelDownload).toHaveBeenCalled())
  })

  it('shows progress while downloading', async () => {
    getEmbeddingModel.mockResolvedValue(status({ state: 'downloading', downloaded_bytes: 100 * MB }))
    show()
    expect(await screen.findByText('100 / 457 MB')).toBeInTheDocument()
    expect(screen.getByText(/searches still work/i)).toBeInTheDocument()
  })

  it('explains an offline failure and offers a retry', async () => {
    getEmbeddingModel.mockResolvedValue(status({ state: 'failed', error: 'offline' }))
    show()
    expect((await screen.findByRole('alert')).textContent).toMatch(/internet connection/i)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('offers nothing when the model is ready', async () => {
    getEmbeddingModel.mockResolvedValue(status({ state: 'ready', downloaded_bytes: 457 * MB }))
    show()
    expect(await screen.findByText(/is installed/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
