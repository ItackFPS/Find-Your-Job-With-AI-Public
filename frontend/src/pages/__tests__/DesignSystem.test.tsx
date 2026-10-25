import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import DesignSystem from '../DesignSystem'
import { ToastProvider } from '../../components'

describe('DesignSystem page', () => {
  it('shows every primitive in a light and a dark panel', () => {
    const { container } = render(<ToastProvider><DesignSystem /></ToastProvider>)
    expect(container.querySelector('[data-theme="light"]')).not.toBeNull()
    expect(container.querySelector('[data-theme="dark"]')).not.toBeNull()
    expect(screen.getAllByRole('button', { name: 'Primary' })).toHaveLength(2)
    expect(screen.getAllByRole('list', { name: 'Example steps' })).toHaveLength(2)
    expect(screen.getAllByRole('alert')).toHaveLength(2)
  })
})
