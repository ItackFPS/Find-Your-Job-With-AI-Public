// Issue #86: an offer several sources list is shown once, with every source it was seen on.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SeenOn } from '../JobDetail'
import type { Job } from '../../api/client'

const base: Job = {
  id: 'j1', source: 'france-travail-web', title: 'Développeur Python', company: 'Acme', location: 'Toulon',
  department: '83', countries: ['France'], remote: false, work_mode: null, employment_type: null,
  salary_min: null, salary_max: null, salary_currency: 'EUR', published_at: null,
  discovered_at: '2026-10-30T08:00:00Z', skills: [], canonical_url: null, apply_url: null,
  status: 'active', application_status: null, score: null,
} as Job

describe('SeenOn', () => {
  it('lists every source, own first, linking those with an address', () => {
    render(<SeenOn job={{ ...base, provenance: [
      { source: 'france-travail-web', url: 'https://candidat.francetravail.fr/offres/1', first_seen_at: '2026-10-29T08:00:00Z', last_seen_at: '2026-10-30T08:00:00Z' },
      { source: 'eures', url: null, first_seen_at: '2026-10-30T08:00:00Z', last_seen_at: '2026-10-30T08:00:00Z' },
    ] }} />)
    expect(screen.getByText(/seen on/i)).toHaveTextContent('Seen on France Travail (website), EURES')
    expect(screen.getByRole('link', { name: 'France Travail (website)' })).toHaveAttribute('href', 'https://candidat.francetravail.fr/offres/1')
    expect(screen.queryByRole('link', { name: 'EURES' })).not.toBeInTheDocument()
  })

  it('falls back to the offer source without provenance', () => {
    render(<SeenOn job={base} />)
    expect(screen.getByText(/seen on/i)).toHaveTextContent('Seen on France Travail (website)')
  })

  it('never links an address that is not http(s)', () => {
    render(<SeenOn job={{ ...base, provenance: [
      { source: 'eures', url: 'javascript:alert(1)', first_seen_at: '2026-10-30T08:00:00Z', last_seen_at: '2026-10-30T08:00:00Z' },
    ] }} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})
