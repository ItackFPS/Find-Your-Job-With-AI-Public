import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SourceStatus } from '../SourceStatus'

describe('SourceStatus', () => {
  it('shows a skipped source with what to set up', () => {
    render(<SourceStatus status="skipped" reason="INDEED_API_KEY is not set." />)
    expect(screen.getByText('Skipped')).toBeInTheDocument()
    expect(screen.getByText('INDEED_API_KEY is not set.')).toBeInTheDocument()
  })

  it('says a fixed skip reason in the interface language, from its key', () => {
    // Phase B B11: the server's English text reached the French UI.
    render(<SourceStatus status="skipped" reason="SERVER TEXT" reasonKey="frozen" />)
    expect(screen.getByText(/Collection is paused while each source is checked/)).toBeInTheDocument()
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument()
  })

  it('shows the recorded reason when the key is unknown', () => {
    render(<SourceStatus status="skipped" reason="kept as is" reasonKey="no_such_key" />)
    expect(screen.getByText('kept as is')).toBeInTheDocument()
  })

  it('tells a failed source apart from a completed one', () => {
    const { rerender } = render(<SourceStatus status="failed" />)
    expect(screen.getByText('Failed')).toBeInTheDocument()
    rerender(<SourceStatus status="completed" />)
    expect(screen.getByText('Completed')).toBeInTheDocument()
    expect(screen.queryByText('Failed')).not.toBeInTheDocument()
  })

  it('claims nothing for runs recorded before statuses existed', () => {
    render(<SourceStatus status={undefined} />)
    expect(screen.getByText('-')).toBeInTheDocument()
    expect(screen.queryByText('Completed')).not.toBeInTheDocument()
  })
})
