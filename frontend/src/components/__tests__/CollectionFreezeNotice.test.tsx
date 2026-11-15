import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { CollectionFreezeNotice } from '../CollectionFreezeNotice'

const getCollectionStatus = vi.fn()
vi.mock('../../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../../api/client')>(),
  getCollectionStatus: () => getCollectionStatus(),
}))

const show = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <CollectionFreezeNotice />
  </QueryClientProvider>,
)

beforeEach(() => getCollectionStatus.mockReset())

describe('CollectionFreezeNotice', () => {
  it('says collection is paused and that offers and applications are kept', async () => {
    getCollectionStatus.mockResolvedValue({ frozen: true, since: '2026-11-14T09:00:00+00:00', reason: 'x', collectors: [] })
    show()
    expect(await screen.findByText('Collection is paused')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/applications are kept/)
  })

  it('shows nothing when collection runs', async () => {
    getCollectionStatus.mockResolvedValue({ frozen: false, since: null, reason: '', collectors: [] })
    const { container } = show()
    await new Promise(r => setTimeout(r, 0))
    expect(container).toBeEmptyDOMElement()
  })
})
