import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatTile } from '../StatTile'

describe('StatTile', () => {
  it('renders the numeric value formatted with locale grouping', () => {
    render(<StatTile label="Collected" value={12345} />)
    // Match digits loosely rather than a literal separator — the grouping
    // character itself (",", " ", a narrow no-break space…) depends on the
    // runtime's default locale, which this test shouldn't assume.
    expect(screen.getByText(/12.345/)).toBeInTheDocument()
  })

  it('renders the label', () => {
    render(<StatTile label="Collected" value={10} />)
    expect(screen.getByText('Collected')).toBeInTheDocument()
  })

  it('renders a string value as-is', () => {
    render(<StatTile label="Status" value="n/a" />)
    expect(screen.getByText('n/a')).toBeInTheDocument()
  })

  it('renders a loading placeholder instead of the value when loading', () => {
    render(<StatTile label="Collected" value={999} loading />)
    expect(screen.queryByText('999')).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading Collected' })).toBeInTheDocument()
  })
})
