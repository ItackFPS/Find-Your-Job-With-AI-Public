// Phase NEXT / I8 regression tests for the personalization/match-factors UI
// added to the job detail page. These render the pure presentational
// sections directly (no router/query-client needed) since they only ever
// receive already-fetched data as props — matching this repo's existing
// pattern of testing small presentational components in isolation.

import { describe, it, expect } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MatchFactorsSection, PersonalizationSection } from '../JobDetail'
import type { JobExplanation } from '../../api/client'

function makeExplanation(overrides: Partial<JobExplanation> = {}): JobExplanation {
  return {
    job_id: 'job-1',
    overall_score: 80,
    score: null,
    filter_result: 'passed',
    rejection_reason: null,
    positive_reasons: [],
    concerns: [],
    rejection_reasons: [],
    personalization_signals: [],
    personalization_adjustment: 0,
    ...overrides,
  }
}

describe('MatchFactorsSection', () => {
  it('renders nothing when there are no factors (legacy job with no score)', () => {
    const { container } = render(<MatchFactorsSection factors={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for an empty factors object', () => {
    const { container } = render(<MatchFactorsSection factors={{}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a labeled row with a percentage for each real backend factor', () => {
    render(
      <MatchFactorsSection
        factors={{
          location_fit: 0.9,
          domain_relevance: 0.5,
          skills_match: 0.25,
        }}
      />
    )
    expect(screen.getByText('Location')).toBeInTheDocument()
    expect(screen.getByText('90%')).toBeInTheDocument()
    expect(screen.getByText('Domain')).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByText('Skills')).toBeInTheDocument()
    expect(screen.getByText('25%')).toBeInTheDocument()
  })

  it('falls back to the raw key for an unrecognized factor name (never hides real backend data)', () => {
    render(<MatchFactorsSection factors={{ some_future_factor: 0.4 }} />)
    expect(screen.getByText('some_future_factor')).toBeInTheDocument()
  })

  it('ignores non-numeric entries instead of crashing (malformed/legacy payload)', () => {
    render(<MatchFactorsSection factors={{ location_fit: 0.7, note: 'n/a' as unknown as number }} />)
    expect(screen.getByText('Location')).toBeInTheDocument()
    expect(screen.queryByText('note')).not.toBeInTheDocument()
  })
})

describe('PersonalizationSection', () => {
  it('shows the empty state when no signals are present (cold start / no feedback yet)', () => {
    render(<PersonalizationSection expl={makeExplanation()} />)
    expect(screen.getByText(/No personalization applied yet/)).toBeInTheDocument()
  })

  it('renders each real signal with its contribution, never fabricating extra ones', () => {
    render(
      <PersonalizationSection
        expl={makeExplanation({
          personalization_adjustment: 0.083,
          personalization_signals: [
            { signal_type: 'company', value: 'Acme Corp', contribution: 0.1, feedback_count: 2 },
            { signal_type: 'skill', value: 'python', contribution: 0.05, feedback_count: 1 },
          ],
        })}
      />
    )
    expect(screen.getByText(/Company: Acme Corp/)).toBeInTheDocument()
    expect(screen.getByText(/Skill: python/)).toBeInTheDocument()
    expect(screen.getByText('+8.3 pts')).toBeInTheDocument()
  })

  it('renders a negative adjustment without the leading plus sign', () => {
    render(
      <PersonalizationSection
        expl={makeExplanation({
          personalization_adjustment: -0.05,
          personalization_signals: [
            { signal_type: 'company', value: 'Bad Fit Inc', contribution: -0.05, feedback_count: 3 },
          ],
        })}
      />
    )
    expect(screen.getByText('-5.0 pts')).toBeInTheDocument()
  })

  it('falls back to the raw signal_type for an unrecognized signal (future backend addition)', () => {
    render(
      <PersonalizationSection
        expl={makeExplanation({
          personalization_adjustment: 0.02,
          personalization_signals: [
            { signal_type: 'future_signal', value: 'x', contribution: 0.02, feedback_count: 1 },
          ],
        })}
      />
    )
    expect(screen.getByText(/future_signal: x/)).toBeInTheDocument()
  })
})

// ---- Feedback panel (P3) ---------------------------------------------------
import { vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { FeedbackPanel } from '../JobDetail'
import type { FeedbackEvent, FeedbackVocabulary } from '../../api/client'

const VOCAB: FeedbackVocabulary = {
  explicit_types: ['interested', 'not_interested', 'ranking_feedback'],
  reasons: ['too_far', 'too_senior', 'interesting_despite_low_score', 'mis_ranked', 'other'],
  allowed_reasons: {
    interested: ['interesting_despite_low_score', 'other'],
    not_interested: ['too_far', 'too_senior', 'mis_ranked', 'other'],
    ranking_feedback: ['mis_ranked', 'interesting_despite_low_score', 'other'],
  },
  canonical_statuses: ['applied'],
}

function evt(overrides: Partial<FeedbackEvent> = {}): FeedbackEvent {
  return {
    event_id: 'e1', job_id: 'job-1', decision_id: null, strategy_version: null, signal_class: 'explicit',
    type: 'not_interested', reasons: ['too_far'], note: null, source: 'ui',
    occurred_at: '2026-10-01T00:00:00+00:00', metadata: {}, ...overrides,
  }
}

describe('FeedbackPanel', () => {
  it('offers the server-provided feedback types and only the reasons allowed for the chosen type', async () => {
    render(<FeedbackPanel vocabulary={VOCAB} events={[]} pending={false} onSubmit={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Interested' })).toBeInTheDocument()
    expect(screen.queryByText('Too far')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Not interested' }))
    expect(screen.getByRole('button', { name: 'Too far' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Interested' }))
    expect(screen.queryByRole('button', { name: 'Too far' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Interesting despite low score' })).toBeInTheDocument()
  })

  it('submits the type, the selected reasons and the note, then resets', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<FeedbackPanel vocabulary={VOCAB} events={[]} pending={false} onSubmit={onSubmit} />)
    await userEvent.click(screen.getByRole('button', { name: 'Not interested' }))
    await userEvent.click(screen.getByRole('button', { name: 'Too far' }))
    await userEvent.click(screen.getByRole('button', { name: 'Too senior' }))
    await userEvent.click(screen.getByRole('button', { name: 'Too senior' })) // toggled off
    await userEvent.type(screen.getByLabelText('Feedback note'), ' long commute ')
    await userEvent.click(screen.getByRole('button', { name: 'Save feedback' }))
    expect(onSubmit).toHaveBeenCalledWith({ type: 'not_interested', reasons: ['too_far'], note: 'long commute' })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save feedback' })).not.toBeInTheDocument())
  })

  it('omits the note when empty', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<FeedbackPanel vocabulary={VOCAB} events={[]} pending={false} onSubmit={onSubmit} />)
    await userEvent.click(screen.getByRole('button', { name: 'Interested' }))
    await userEvent.type(screen.getByLabelText('Feedback note'), '   ')
    await userEvent.click(screen.getByRole('button', { name: 'Save feedback' }))
    expect(onSubmit).toHaveBeenCalledWith({ type: 'interested', reasons: [], note: undefined })
  })

  it('keeps the selection and the note and shows an error when saving fails', async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error('boom'))
    render(<FeedbackPanel vocabulary={VOCAB} events={[]} pending={false} onSubmit={onSubmit} />)
    await userEvent.click(screen.getByRole('button', { name: 'Not interested' }))
    await userEvent.click(screen.getByRole('button', { name: 'Too far' }))
    await userEvent.type(screen.getByLabelText('Feedback note'), 'keep me')
    await userEvent.click(screen.getByRole('button', { name: 'Save feedback' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save')
    expect(screen.getByLabelText('Feedback note')).toHaveValue('keep me')
    expect(screen.getByRole('button', { name: 'Too far' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('says plainly that feedback does not change the ranking yet', () => {
    render(<FeedbackPanel vocabulary={VOCAB} events={[]} pending={false} onSubmit={vi.fn()} />)
    expect(screen.getByText(/does not change your ranking yet/)).toBeInTheDocument()
  })

  it('shows the last explicit feedback only', () => {
    render(
      <FeedbackPanel
        vocabulary={VOCAB}
        events={[evt({ signal_class: 'behavior', type: 'viewed', reasons: [] }), evt()]}
        pending={false}
        onSubmit={vi.fn()}
      />
    )
    expect(screen.getByText(/Last: Not interested/)).toHaveTextContent('Too far')
  })

  it('disables saving while a submission is pending', async () => {
    render(<FeedbackPanel vocabulary={VOCAB} events={[]} pending={true} onSubmit={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Interested' }))
    expect(screen.getByRole('button', { name: 'Save feedback' })).toBeDisabled()
  })
})

// ---- Decision panel (P1e) ----------------------------------------------------
import { DecisionPanel } from '../JobDetail'
import type { DecisionRecord, DecisionEvidence } from '../../api/client'

function ev(over: Partial<DecisionEvidence>): DecisionEvidence {
  return {
    dimension: 'skills', polarity: 'positive', state: 'satisfied', message: 'm', observed: null, expected: null,
    weight: null, span: null,
    source: { stage: 'filter', rule_id: 'r', rule_version: null, evidence_source: null }, ...over,
  }
}

const DECISION: DecisionRecord = {
  decision_id: 'abcdef123456', run_id: 'run00000999', job_id: 'j1', decided_at: '2026-10-01T00:00:00+00:00',
  outcome: 'accepted', deciding_stage: 'ranking', strategy_version: 'sv-1', fit_score: 0.83,
  evidence: [
    ev({ message: 'Python matches', polarity: 'positive' }),
    ev({ message: 'Too senior', polarity: 'negative', state: 'violated', dimension: 'seniority',
         span: { field: 'description', start: 0, end: 5, text: '5 ans', extractor_id: 'x' } }),
    ev({ message: 'No salary data', polarity: 'unknown', state: 'unknown', dimension: 'salary' }),
  ],
  conflicts: [{ dimensions: ['work_mode'], description: 'Remote vs on-site' }],
  stage_trace: [], exposure: null,
  summary: { supporting: 1, against: 1, unknown: 1, conflicts: 1, headline: 'accepted at ranking (score 0.83): 1 supporting' },
}

describe('DecisionPanel — confidence and review', () => {
  const withAssessment = (over: Partial<DecisionRecord>): DecisionRecord => ({
    ...DECISION, decision_confidence: 0.4321, confidence_calibrated: false,
    signals: { assessment: { state: 'review', confidence: 0.43, reasons: ['signals disagree: rules 0.82 vs phase2 0.55'],
                             unknowns: [], mode: 'shadow', calibrated: false } },
    ...over,
  })

  it('shows the agreement-based confidence as uncalibrated and the shadow review suggestion', () => {
    render(<DecisionPanel decision={withAssessment({})} />)
    expect(screen.getByText(/Decision confidence 0\.43 \(agreement-based, not calibrated\)/)).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('Would be flagged for review (shadow mode)')
    expect(screen.getByRole('note')).toHaveTextContent('signals disagree')
    expect(screen.getByRole('note')).toHaveTextContent('stays ranked and visible')
  })

  it('says "Flagged for review" once the outcome itself is review, and shows nothing for accept', () => {
    const { rerender } = render(<DecisionPanel decision={withAssessment({ outcome: 'review' })} />)
    expect(screen.getByRole('note')).toHaveTextContent('Flagged for review')
    rerender(<DecisionPanel decision={withAssessment({ signals: { assessment: { state: 'accept', confidence: 0.9, reasons: [],
      unknowns: [], mode: 'shadow', calibrated: false } } })} />)
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })
})

describe('DecisionPanel', () => {
  it('shows the headline and groups evidence into supporting / against / unknown / conflicts', () => {
    render(<DecisionPanel decision={DECISION} />)
    expect(screen.getByText(/accepted at ranking/)).toBeInTheDocument()
    expect(screen.getByText('SUPPORTING (1)')).toBeInTheDocument()
    expect(screen.getByText('AGAINST (1)')).toBeInTheDocument()
    expect(screen.getByText('UNKNOWN (1)')).toBeInTheDocument()
    expect(screen.getByText('CONFLICTS (1)')).toBeInTheDocument()
    expect(screen.getByText(/Too senior/)).toHaveTextContent('5 ans')       // the quoted evidence span
    expect(screen.getByText(/No salary data/)).toBeInTheDocument()
    expect(screen.getByText('Remote vs on-site')).toBeInTheDocument()
  })

  it('omits empty groups and shows the decision/run ids', () => {
    render(<DecisionPanel decision={{ ...DECISION, evidence: [ev({})], conflicts: [] }} />)
    expect(screen.queryByText(/AGAINST/)).not.toBeInTheDocument()
    expect(screen.queryByText(/CONFLICTS/)).not.toBeInTheDocument()
    expect(screen.getByText(/Decision abcdef12 · run run00000/)).toBeInTheDocument()
  })
})
