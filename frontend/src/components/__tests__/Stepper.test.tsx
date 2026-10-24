import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Stepper } from '../Stepper'

const STEPS = [{ key: 'a', label: 'One' }, { key: 'b', label: 'Two' }, { key: 'c', label: 'Three' }]

describe('Stepper', () => {
  it('marks the current step with aria-current and earlier steps as complete', () => {
    render(<Stepper steps={STEPS} current={1} label="Setup" />)
    const items = screen.getAllByRole('listitem')
    expect(screen.getByRole('list', { name: 'Setup' })).toBeInTheDocument()
    expect(items[0]).toHaveClass('fyj-stepper__step--complete')
    expect(items[1]).toHaveAttribute('aria-current', 'step')
    expect(items[2]).toHaveClass('fyj-stepper__step--upcoming')
    expect(items[2]).not.toHaveAttribute('aria-current')
  })
})
