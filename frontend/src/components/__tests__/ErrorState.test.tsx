import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ErrorState } from '../ErrorState'

describe('ErrorState', () => {
  it('announces the error and offers the recovery action', async () => {
    const onRetry = vi.fn()
    render(<ErrorState message="Could not load" details="HTTP 500" onRetry={onRetry} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load')
    expect(screen.getByText('HTTP 500')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('renders no button when there is no recovery action', () => {
    render(<ErrorState message="Gone" />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
