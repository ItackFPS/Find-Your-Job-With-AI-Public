import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Chip } from '../Chip'

describe('Chip', () => {
  it('renders the supplied text', () => {
    render(<Chip>Remote</Chip>)
    expect(screen.getByText('Remote')).toBeInTheDocument()
  })

  it('renders composite children (e.g. a country + count) as supplied', () => {
    render(<Chip>France <strong>42</strong></Chip>)
    expect(screen.getByText('France')).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
  })
})
