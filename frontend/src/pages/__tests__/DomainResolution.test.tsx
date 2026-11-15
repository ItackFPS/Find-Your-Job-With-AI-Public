// Phase B B5-C: a typed line-of-work label the vocabulary does not know is shown as such.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { DomainPicker } from '../Profile'

const posted: unknown[] = []

vi.mock('axios', () => {
  const client = {
    get: async () => ({ data: [] }),
    post: async (url: string, body: { labels: string[] }) => {
      posted.push({ url, body })
      return { data: { labels: body.labels.map(label => ({
        label, status: label === 'Boulangerie' ? 'unresolved' : 'resolved',
        families: label === 'Boulangerie' ? [] : ['systems'] })) } }
    },
    put: async () => ({ data: {} }),
    patch: async () => ({ data: {} }),
  }
  return { default: { create: () => client } }
})

describe('DomainPicker unresolved labels', () => {
  beforeEach(() => { posted.length = 0 })

  it('marks only the labels the vocabulary does not know', async () => {
    render(<DomainPicker values={['systems', 'Administration systèmes', 'Boulangerie']} onChange={() => {}} />)
    const note = await screen.findByTestId('domains-unresolved')
    expect(note).toHaveTextContent('Boulangerie')
    expect(note).not.toHaveTextContent('Administration')
    expect(screen.getByText('Boulangerie').closest('[data-unresolved]')).not.toBeNull()
    expect(screen.getByText('Administration systèmes').closest('[data-unresolved]')).toBeNull()
    // curated keys are known without asking
    expect(posted).toEqual([{ url: '/profile/domains/resolve', body: { labels: ['Administration systèmes', 'Boulangerie'] } }])
  })

  it('asks nothing and shows nothing for curated keys only', async () => {
    render(<DomainPicker values={['systems']} onChange={() => {}} />)
    await new Promise(r => setTimeout(r, 400))
    expect(posted).toEqual([])
    expect(screen.queryByTestId('domains-unresolved')).toBeNull()
  })

  it('drops the note when the label is removed', async () => {
    const { rerender } = render(<DomainPicker values={['Boulangerie']} onChange={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('domains-unresolved')).toBeInTheDocument())
    rerender(<DomainPicker values={[]} onChange={() => {}} />)
    await waitFor(() => expect(screen.queryByTestId('domains-unresolved')).toBeNull())
  })
})
