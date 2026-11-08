import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Sidebar } from '../Sidebar'

function show(path = '/') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}><Sidebar /></MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Sidebar', () => {
  it('shows the everyday pages and keeps the advanced ones folded', () => {
    show()
    expect(screen.getByRole('link', { name: 'Jobs' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Pipeline' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Advanced' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('opens the advanced group from the keyboard', async () => {
    show()
    screen.getByRole('button', { name: 'Advanced' }).focus()
    await userEvent.keyboard('{Enter}')
    expect(screen.getByRole('link', { name: 'Pipeline' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Advanced' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('stays open on an advanced page', () => {
    show('/stats')
    expect(screen.getByRole('link', { name: 'Stats' })).toBeInTheDocument()
  })
})
