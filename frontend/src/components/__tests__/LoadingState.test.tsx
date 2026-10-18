import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LoadingState } from '../LoadingState'

describe('LoadingState', () => {
  it('renders the default loading message', () => {
    render(<LoadingState />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders a custom message when supplied', () => {
    render(<LoadingState message="Connecting…" />)
    expect(screen.getByText('Connecting…')).toBeInTheDocument()
  })

  it('exposes an accessible status role', () => {
    render(<LoadingState />)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
