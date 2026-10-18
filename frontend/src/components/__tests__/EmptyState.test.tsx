import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { EmptyState } from '../EmptyState'

describe('EmptyState', () => {
  it('renders the supplied message', () => {
    render(<EmptyState message="No jobs found." />)
    expect(screen.getByText('No jobs found.')).toBeInTheDocument()
  })

  it('renders an optional action', () => {
    render(<EmptyState message="No runs yet." action={<button>Run pipeline</button>} />)
    expect(screen.getByRole('button', { name: 'Run pipeline' })).toBeInTheDocument()
  })
})
