// Phase B B6-C: an experience, education or salary requirement says where it came from.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { DecisionPanel } from '../JobDetail'
import { provenanceOf } from '../decisionProvenance'
import type { DecisionEvidence, DecisionRecord } from '../../api/client'

const ev = (over: Partial<DecisionEvidence>): DecisionEvidence => ({
  dimension: 'experience', polarity: 'negative', state: 'violated', message: 'Job requires 5 years.',
  observed: '5', expected: '2', weight: null,
  source: { stage: 'requirements_gate', rule_id: 'gate.experience', rule_version: null, evidence_source: 'heuristic' },
  span: null, ...over,
} as DecisionEvidence)

describe('provenanceOf', () => {
  it('reads a structured source field, offer text, or not stated', () => {
    expect(provenanceOf(ev({ source: { stage: 's', rule_id: 'r', rule_version: null, evidence_source: 'metadata' } }))).toBe('structured')
    expect(provenanceOf(ev({}))).toBe('text')
    expect(provenanceOf(ev({ source: { stage: 's', rule_id: 'r', rule_version: null, evidence_source: null },
      span: { field: 'description', start: 0, end: 5, text: '5 ans', extractor_id: 'x' } } as never))).toBe('text')
    expect(provenanceOf(ev({ state: 'unknown', polarity: 'unknown' }))).toBe('unknown')
    expect(provenanceOf(ev({ dimension: 'education', source: { stage: 's', rule_id: 'r', rule_version: null, evidence_source: 'rule' } }))).toBe('unknown')
  })

  it('says nothing for other dimensions', () => {
    expect(provenanceOf(ev({ dimension: 'location' }))).toBeNull()
    expect(provenanceOf(ev({ dimension: 'skills' }))).toBeNull()
  })
})

describe('DecisionPanel', () => {
  it('shows the provenance next to an experience requirement only', () => {
    const decision = {
      decision_id: 'd1', run_id: 'r1', job_id: 'j1', decided_at: '2026-11-15T00:00:00+00:00', outcome: 'rejected',
      deciding_stage: 'requirements_gate', strategy_version: 'sv', fit_score: null, decision_confidence: null,
      confidence_calibrated: false, signals: {}, conflicts: [], stage_trace: [], exposure: null,
      summary: { supporting: 0, against: 2, unknown: 0, conflicts: 0, headline: 'rejected' },
      evidence: [ev({}), ev({ dimension: 'location', message: 'Too far.' })],
    } as unknown as DecisionRecord
    render(<MemoryRouter><DecisionPanel decision={decision} /></MemoryRouter>)
    const labels = screen.getAllByTestId('evidence-provenance')
    expect(labels).toHaveLength(1)
    expect(labels[0]).toHaveTextContent('read in the offer text')
  })
})
