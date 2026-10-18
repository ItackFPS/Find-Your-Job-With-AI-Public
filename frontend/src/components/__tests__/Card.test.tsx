import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Card } from '../Card'

describe('Card', () => {
  it('renders its children content', () => {
    render(<Card>Hello content</Card>)
    expect(screen.getByText('Hello content')).toBeInTheDocument()
  })

  it('renders a title and subtitle when provided', () => {
    render(<Card title="Latest run" subtitle="a moment ago">body</Card>)
    expect(screen.getByText('Latest run')).toBeInTheDocument()
    expect(screen.getByText('a moment ago')).toBeInTheDocument()
  })

  it('renders header actions when provided', () => {
    render(<Card title="Run" actions={<span>Action</span>}>body</Card>)
    expect(screen.getByText('Action')).toBeInTheDocument()
  })

  it('omits the header row entirely when no header props are given', () => {
    const { container } = render(<Card>just body</Card>)
    expect(container.textContent).toBe('just body')
  })
})
