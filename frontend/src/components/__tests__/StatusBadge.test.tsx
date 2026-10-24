import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge } from '../StatusBadge'

describe('StatusBadge', () => {
  it.each(['completed', 'failed', 'running'])('renders the %s status used by the backend', (status) => {
    render(<StatusBadge status={status} />)
    expect(screen.getByText(new RegExp(`^${status}$`, 'i'))).toBeInTheDocument()
  })

  it.each(['pausing', 'paused', 'interrupted'])('gives the %s scanner state its own color', (status) => {
    render(<StatusBadge status={status} />)
    expect(screen.getByText(new RegExp(`^${status}$`, 'i')).style.color).not.toBe('var(--color-muted)')
  })

  it('falls back gracefully for an unrecognized status rather than crashing', () => {
    render(<StatusBadge status="something_else" />)
    expect(screen.getByText('something_else')).toBeInTheDocument()
  })
})
