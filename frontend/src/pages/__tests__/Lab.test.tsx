import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ActiveStrategyView, ExperimentCard, RunRow, SelfCheckFeedbackView, SelfCheckView } from '../Lab'
import type {
  ActiveStrategy, EvalRunSummary, LabExperiment, SelfCheckDataset, SelfCheckResponse,
} from '../../api/client'

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

  it('says when no self-check has been recorded', () => {
    render(<SelfCheckView data={{ latest: null, history: [], unreadable: 0 }} />)
    expect(screen.getByText(/No self-check recorded yet/)).toBeInTheDocument()
  })

  it('shows a self-check safety violation, its patterns, and actions apart from suggestions', () => {
    const entry: SelfCheckDataset = {
      status: 'safety_violation',
      dataset: { id: 'synthetic-ai-filter-50', version: '1', kind: 'synthetic', n_jobs: 50, n_labelled: 50,
        accept_labels: 22, label_provenance: { human: 50 } },
      strategy: { name: 'current', fingerprint: 'f'.repeat(64) },
      safety: { unsafe_rejections: [{ job_id: 'bm01', reasons: ['filter:title_keywords'] }], rejections_without_reason: [] },
      eligibility: { tp: 21, fp: 3, fn: 1, tn: 24, precision: 0.875, recall: 0.955 },
      explanations: { rejections: 25, with_reason: 25, coverage: 1 },
      ranking: { status: 'not_measured', why: 'eligibility labels only', score_separation: { status: 'diagnostic', pairwise_auc: 0.9 } },
      baseline: { status: 'differs', flips: { fixed: [], broken: ['bm01'], unjudged: [] } },
      patterns: [{ kind: 'unsafe_rejection', key: 'filter:title_keywords', count: 1, job_ids: ['bm01'],
        example_reasons: ['filter:title_keywords'], lever: { kind: 'lab', detail: 'attribute' } }],
      limitations: ['synthetic, author-written postings'],
    }
    const data: SelfCheckResponse = {
      unreadable: 0,
      latest: { created_at: '2026-10-18T10:00:00+00:00', status: 'safety_violation', generated_by: 'deterministic',
        datasets: [entry], next_steps: '',
        recommendations: [{ kind: 'action', dataset: 'synthetic-ai-filter-50', text: 'Release blocker: bm01' },
          { kind: 'suggestion', dataset: 'synthetic-ai-filter-50', text: 'run the lab agent' }] },
      history: [{ file: 'b.json', created_at: '2026-10-18T10:00:00+00:00', status: 'safety_violation', datasets: {} },
        { file: 'a.json', created_at: '2026-10-17T10:00:00+00:00', status: 'pass', datasets: {} }],
    }
    render(<SelfCheckView data={data} />)
    expect(screen.getAllByText('safety violation')).toHaveLength(2)
    expect(screen.getByText(/0 fixed · 1 broken/)).toBeInTheDocument()
    expect(screen.getByText('filter:title_keywords')).toBeInTheDocument()
    expect(screen.getByText(/Ranking quality: not measured/)).toBeInTheDocument()
    expect(screen.getByText('action')).toBeInTheDocument()
    expect(screen.getByText('suggestion')).toBeInTheDocument()
    expect(screen.getByText(/Earlier: 2026-10-17 pass/)).toBeInTheDocument()
  })

  it('shows feedback as signals, with disagreements as leads, never as labels', () => {
    render(<SelfCheckFeedbackView fb={{
      status: 'measured', decided_jobs: 40, jobs_with_explicit_feedback: 4, coverage: 0.1,
      latest_answers: { interested: 2, not_interested: 1, unsure: 1 }, decisions_with_evidence: 40,
      explanation_coverage: 1,
      contradictions: { rejected_but_interested: { count: 1, job_ids: ['j1'] },
        accepted_but_not_interested: { count: 0, job_ids: [] } },
      note: 'signals',
    }} />)
    expect(screen.getByText(/4 of 40 decided \(10%\)/)).toBeInTheDocument()
    expect(screen.getByText(/2 interested, 1 not interested, 1 unsure/)).toBeInTheDocument()
    expect(screen.getByText(/1 rejected but interested/)).toBeInTheDocument()
    expect(screen.getByText(/signals, not verified labels/)).toBeInTheDocument()
  })

  it('says when feedback could not be measured', () => {
    render(<SelfCheckFeedbackView fb={{ status: 'unavailable', why: 'database not found: jobs.db' }} />)
    expect(screen.getByText(/Feedback: not measured \(database not found/)).toBeInTheDocument()
  })

  it('tells a human what an experiment recommends without acting on it', () => {
    const exp: LabExperiment = {
      experiment_id: 'exp-3', created_at: '2026-10-14T03:00:00+00:00', candidate_id: 'c', dataset_id: 'd',
      verdict: 'regression', eligible_for_approval: false, candidate_status: 'candidate', hypothesis: 'h3',
      overrides: {}, explanation: null, fixed: 0, broken: 3, guardrails: {}, delta: {}, limitations: [],
      promotion_evidence: 'not_eligible', independently_validated: false, evidence_basis: null, next_action: 'reject',
    }
    render(<ExperimentCard exp={exp} />)
    expect(screen.getByText('next: discard')).toBeInTheDocument()
  })
})
