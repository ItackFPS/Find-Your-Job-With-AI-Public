import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { ReviewCard } from '../Review'
import type { ReviewItem } from '../../api/client'

function item(overrides: Partial<ReviewItem['decision']> = {}, extra: Partial<ReviewItem> = {}): ReviewItem {
  return {
    job_stored: false,
    has_snapshot: true,
    ...extra,
    decision: {
      decision_id: 'd1', run_id: 'r1', job_id: 'j1', decided_at: '2026-10-01T00:00:00+00:00',
      outcome: 'rejected', deciding_stage: 'filter', strategy_version: 'sv-1', fit_score: null,
      decision_confidence: 0.5, confidence_calibrated: false,
      signals: { assessment: { state: 'review', confidence: 0.5, reasons: ["the user's own feedback (explicit:interested) says they want this job"], conflicts: [], unknowns: [], calibrated: false, mode: 'shadow' } as never },
      evidence: [], conflicts: [], stage_trace: [], exposure: null,
      summary: { supporting: 0, against: 0, unknown: 0, conflicts: 0, headline: 'rejected at filter' },
      job: { title: 'Technicien support', company: 'Company-1', source: 'src', location: 'Toulon' },
      ...overrides,
    },
  }
}

describe('ReviewCard', () => {
  it('shows a rejected job with the reason it needs a human, without linking to a missing detail page', () => {
    render(<MemoryRouter><ReviewCard item={item()} pending={false} onAnswer={() => {}} /></MemoryRouter>)
    expect(screen.getByText('Technicien support')).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('Rejected at Hard Filter')).toBeInTheDocument()
    expect(screen.getByText(/explicit:interested/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Posting' })).toBeInTheDocument()
  })

  it('links a stored job to its detail page and sends the chosen answer', async () => {
    const onAnswer = vi.fn()
    render(<MemoryRouter><ReviewCard item={item({ outcome: 'accepted', deciding_stage: 'ranking' }, { job_stored: true, has_snapshot: false })}
      pending={false} onAnswer={onAnswer} /></MemoryRouter>)
    expect(screen.getByRole('link', { name: 'Technicien support' })).toHaveAttribute('href', '/jobs/j1')
    expect(screen.queryByRole('button', { name: 'Posting' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Not for me' }))
    expect(onAnswer).toHaveBeenCalledWith('not_interested')
  })

  it('explains an audit-sampled rejection with its rule and evidence, and accepts "Not sure"', async () => {
    const onAnswer = vi.fn()
    render(<MemoryRouter><ReviewCard item={item({
      deciding_stage: 'requirements_gate',
      signals: { audit: { sampled: true, sampler: 'rejection-audit.v1', stratum: 'requirements_gate:education', stratum_size: 4, rank_in_stratum: 1 } },
      evidence: [{
        dimension: 'education', polarity: 'negative', state: 'violated', message: 'Job requires Bac+5 (mandatory); candidate maximum is bac+2.',
        observed: 'Bac+5', expected: 'bac+2', weight: null,
        source: { stage: 'requirements_gate', rule_id: 'gate.education', rule_version: 'hard-filter.v3', evidence_source: 'heuristic' },
        span: { field: 'description', start: 0, end: 5, text: 'Bac+5 requis', extractor_id: 'education.bacp5.v1' },
      }] as never,
    })} pending={false} onAnswer={onAnswer} /></MemoryRouter>)
    expect(screen.getByText('Audit sample')).toBeInTheDocument()
    expect(screen.getByText('Requirements Gate · education')).toBeInTheDocument()
    expect(screen.getByText(/Job requires Bac\+5 \(mandatory\)/)).toBeInTheDocument()
    expect(screen.getByText(/Bac\+5 requis/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Not sure' }))
    expect(onAnswer).toHaveBeenCalledWith('unsure')
  })
})
