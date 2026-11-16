// Issue #253: the Jobs page says why the list holds what it holds.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ListStatusNotice } from '../ListStatusNotice'
import type { ListStatus } from '../../api/client'

const base: ListStatus = {
  visible_run_id: 'r1', visible_run_completed_at: null, latest_run_id: 'r1', latest_run_status: 'completed',
  latest_run_outcome: 'successful', previous_list_kept: false, incomplete_sources: {}, carried_by_source: {},
  listed: 10, hidden_by_user: 0, decouverte: 2, outside_criteria: 0, source_off: {}, expired: {},
}
const show = (s: ListStatus | null) => render(<MemoryRouter><ListStatusNotice status={s} /></MemoryRouter>)

describe('ListStatusNotice', () => {
  it('says nothing when there is nothing to explain', () => {
    show(base)
    expect(screen.queryByTestId('list-status')).not.toBeInTheDocument()
    show(null)
    expect(screen.queryByTestId('list-status')).not.toBeInTheDocument()
  })

  it('names incomplete sources with their reason and counts carried offers', () => {
    show({ ...base, incomplete_sources: { eures: 'suspect_empty', indeed: 'failed' }, carried_by_source: { eures: 40, indeed: 3 } })
    expect(screen.getByTestId('list-status-incomplete')).toHaveTextContent('eures returned no offer, indeed failed')
    expect(screen.getByTestId('list-status-carried')).toHaveTextContent('43 offers in the list come from earlier searches')
  })

  it('says the previous list was kept, and why', () => {
    show({ ...base, previous_list_kept: true, latest_run_status: 'failed' })
    expect(screen.getByTestId('list-status-kept')).toHaveTextContent('The last search failed')
  })

  it('counts offers hidden by a source that is off and links to Settings', () => {
    show({ ...base, source_off: { indeed: 5, linkedin: 1 } })
    expect(screen.getByTestId('list-status-off')).toHaveTextContent('6 offers are hidden because their source is off (indeed, linkedin)')
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings')
  })

  it('counts expired offers, stale ones apart', () => {
    show({ ...base, expired: { stale: 4, valid_through: 1 } })
    expect(screen.getByTestId('list-status-expired')).toHaveTextContent('5 expired offers are no longer shown, 4 of them not seen for 30 days')
  })
})
