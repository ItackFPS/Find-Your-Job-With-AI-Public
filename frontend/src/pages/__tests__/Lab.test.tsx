import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ActiveStrategyView, ExperimentCard, RunRow } from '../Lab'
import type { ActiveStrategy, EvalRunSummary, LabExperiment } from '../../api/client'

describe('Lab', () => {
  it('shows an experiment verdict, its guardrails, deltas and limits', () => {
    const exp: LabExperiment = {
      experiment_id: 'exp-1', created_at: '2026-10-14T03:00:00+00:00', candidate_id: 'cand-1', dataset_id: 'real-v1',
      verdict: 'insufficient_evidence', eligible_for_approval: false, candidate_status: 'candidate',
      hypothesis: "exclude 'defense'", overrides: {}, explanation: '250 labelled, 4 discordant pairs (need 5)',
      fixed: 4, broken: 0, guardrails: { no_new_unsafe_rejection: true, latency: false },
      delta: { precision: 0.0019, recall: 0, unsafe_rejection_rate: 0, fp: -4, fn: 0 },
      limitations: ['only 10 accept label(s)'], promotion_evidence: 'not_eligible', independently_validated: false,
      evidence_basis: 'mixed',
    }
    render(<ExperimentCard exp={exp} />)
    expect(screen.getByText("exclude 'defense'")).toBeInTheDocument()
    expect(screen.getByText('insufficient evidence')).toBeInTheDocument()
    expect(screen.queryByText('eligible for human approval')).toBeNull()
    expect(screen.getByText('✓ no new unsafe rejection')).toBeInTheDocument()
    expect(screen.getByText('✗ latency')).toBeInTheDocument()
    expect(screen.getByText(/Δfp -4/)).toBeInTheDocument()
    expect(screen.getByText('only 10 accept label(s)')).toBeInTheDocument()
    expect(screen.queryByText(/independently supported|exploratory only/)).toBeNull()
  })

  it('never shows an eligible experiment backed only by assisted labels as independently validated', () => {
    const base: LabExperiment = {
      experiment_id: 'exp-2', created_at: '2026-10-14T03:00:00+00:00', candidate_id: 'cand-2', dataset_id: 'real-v1',
      verdict: 'improvement', eligible_for_approval: true, candidate_status: 'candidate', hypothesis: 'h',
      overrides: {}, explanation: null, fixed: 9, broken: 0, guardrails: {}, delta: {}, limitations: [],
      promotion_evidence: 'exploratory_only', independently_validated: false, evidence_basis: 'not_independent',
    }
    const { unmount } = render(<ExperimentCard exp={base} />)
    expect(screen.getByText('eligible for human approval')).toBeInTheDocument()
    expect(screen.getByText('exploratory only — not independently validated')).toBeInTheDocument()
    expect(screen.queryByText('independently supported')).toBeNull()
    unmount()
    render(<ExperimentCard exp={{ ...base, promotion_evidence: 'independently_supported', independently_validated: true }} />)
    expect(screen.getByText('independently supported')).toBeInTheDocument()
    expect(screen.queryByText(/exploratory only/)).toBeNull()
  })

  it('renders a run row with its unsafe rejection rate', () => {
    const run: EvalRunSummary = {
      file: 'real-v1/r.json', run_id: 'r', created_at: '2026-10-14T01:09:59+00:00',
      dataset: { id: 'real-v1', version: '2', kind: 'real', jobs_sha256: 'j', labels_sha256: 'l' },
      strategy: { name: 'current', fingerprint: '2e1fd5b3c584aaaa' }, n_labelled: 250,
      overall: { tp: 7, tn: 115, fp: 116, fn: 3, borderline: 9, accuracy: 0.506, precision: 0.057, recall: 0.7, unsafe_rejection_rate: 0.3 },
      review_status: null, warnings: [],
    }
    render(<table><tbody><RunRow run={run} /></tbody></table>)
    expect(screen.getByText('real-v1@2')).toBeInTheDocument()
    expect(screen.getByText('30%')).toBeInTheDocument()
    expect(screen.getByText('tp 7 · fp 116 · fn 3 · tn 115')).toBeInTheDocument()
  })

  it('says which strategy the pipeline actually runs', () => {
    const stale: ActiveStrategy = {
      current: { pointer_id: 'ptr-1', bundle_hash: 'abcdef1234567890', action: 'approve', approved_by: 'reviewer' },
      history: [], honoured_by_pipeline: true, note: 'The pipeline runs on config/: config changed.',
      pipeline: { status: 'stale', reason: 'config changed', pointer_id: 'ptr-1', bundle_hash: 'abcdef1234567890',
        action: 'approve', approved_by: 'reviewer', approval_basis: 'independently_supported' },
    }
    const { unmount } = render(<ActiveStrategyView data={stale} />)
    expect(screen.getByText(/Pointer ptr-1 → bundle abcdef123456/)).toBeInTheDocument()
    expect(screen.getByText('pipeline: config/ (stale)')).toBeInTheDocument()
    unmount()
    render(<ActiveStrategyView data={{ ...stale, pipeline: { ...stale.pipeline, status: 'active' } }} />)
    expect(screen.getByText('pipeline: approved bundle (active)')).toBeInTheDocument()
    expect(screen.queryByText(/exploratory evidence|not recorded/)).toBeNull()
  })

  it('keeps an exploratory approval visible while it is live', () => {
    const live: ActiveStrategy = {
      current: { pointer_id: 'ptr-2', bundle_hash: 'abcdef1234567890', action: 'approve', approved_by: 'reviewer' },
      history: [], honoured_by_pipeline: true, note: '',
      pipeline: { status: 'active', reason: 'approved', pointer_id: 'ptr-2', bundle_hash: 'abcdef1234567890',
        action: 'approve', approved_by: 'reviewer', approval_basis: 'exploratory_only' },
    }
    const { unmount } = render(<ActiveStrategyView data={live} />)
    expect(screen.getByText('approved on exploratory evidence — not independently validated')).toBeInTheDocument()
    unmount()
    render(<ActiveStrategyView data={{ ...live, pipeline: { ...live.pipeline, approval_basis: 'unrecorded' } }} />)
    expect(screen.getByText('approval evidence not recorded')).toBeInTheDocument()
  })
})
