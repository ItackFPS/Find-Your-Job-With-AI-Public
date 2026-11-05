import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AlertText } from '../OpsAlerts'

const base = { alert_id: 'a', raised_at: '2026-11-05T08:00:00+00:00', updated_at: '2026-11-05T08:00:00+00:00',
  severity: 'warning', status: 'open' as const, resolved_at: null, resolved_by: null }

describe('AlertText', () => {
  it('words a volume drop with its evidence and what to check', () => {
    render(<AlertText a={{ ...base, scope: 'source:remotive', code: 'source_volume_drop',
      evidence: { source: 'remotive', recent: [0, 0], median: 41, runs: 7 } }} />)
    expect(screen.getByText('remotive brings far fewer offers than usual')).toBeInTheDocument()
    expect(screen.getByText('Last runs: 0, 0 offers; usually about 41 (over 7 runs).')).toBeInTheDocument()
    expect(screen.getByText(/Settings › Sources/)).toBeInTheDocument()
  })

  it('words a completeness drop with the field name', () => {
    render(<AlertText a={{ ...base, scope: 'source:remotive:field:company', code: 'field_completeness_drop',
      evidence: { source: 'remotive', field: 'company', today: 0, median: 1, offers: 12 } }} />)
    expect(screen.getByText('remotive: the company is missing on many more offers')).toBeInTheDocument()
    expect(screen.getByText("Filled on 0 % of today's 12 offers, usually 100 %.")).toBeInTheDocument()
  })
})
