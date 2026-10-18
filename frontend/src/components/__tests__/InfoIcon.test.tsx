import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoIcon } from '../InfoIcon'

describe('InfoIcon', () => {
  it('renders as an accessible interactive button', () => {
    render(<InfoIcon label="More about this metric" />)
    expect(screen.getByRole('button', { name: 'More about this metric' })).toBeInTheDocument()
  })

  it('is keyboard-operable: focusing and activating reveals the popover content', async () => {
    const user = userEvent.setup()
    render(<InfoIcon label="Score info">Explanation text</InfoIcon>)

    expect(screen.queryByText('Explanation text')).not.toBeInTheDocument()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Score info' })).toHaveFocus()
    expect(screen.getByText('Explanation text')).toBeInTheDocument()
  })

  it('closes on Escape', async () => {
    const user = userEvent.setup()
    render(<InfoIcon label="Score info">Explanation text</InfoIcon>)

    await user.tab()
    expect(screen.getByText('Explanation text')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByText('Explanation text')).not.toBeInTheDocument()
  })

  it('is inert (disabled) when no content is supplied yet', () => {
    render(<InfoIcon label="Placeholder" />)
    expect(screen.getByRole('button', { name: 'Placeholder' })).toBeDisabled()
  })
})
