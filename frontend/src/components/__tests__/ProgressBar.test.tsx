import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ProgressBar } from '../ProgressBar'

describe('ProgressBar', () => {
  it('exposes the correct accessible progress value when determinate', () => {
    render(<ProgressBar value={42} label="Scoring" />)
    const bar = screen.getByRole('progressbar', { name: 'Scoring' })
    expect(bar).toHaveAttribute('aria-valuenow', '42')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
  })

  it('clamps out-of-range values into 0-100', () => {
    render(<ProgressBar value={150} label="Over" />)
    expect(screen.getByRole('progressbar', { name: 'Over' })).toHaveAttribute('aria-valuenow', '100')
  })

  it('marks itself busy and omits aria-valuenow when indeterminate', () => {
    render(<ProgressBar indeterminate label="Working" />)
    const bar = screen.getByRole('progressbar', { name: 'Working' })
    expect(bar).toHaveAttribute('aria-busy', 'true')
    expect(bar).not.toHaveAttribute('aria-valuenow')
  })

  it('renders the optional right label', () => {
    render(<ProgressBar value={50} label="Scoring" rightLabel="50%" />)
    expect(screen.getByText('50%')).toBeInTheDocument()
  })
})
