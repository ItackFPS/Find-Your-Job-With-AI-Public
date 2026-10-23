import type { RunStats } from '../api/client'

// ---------------------------------------------------------------------------
// Canonical pipeline stage definition.
//
// This is the ONE place that knows the pipeline's stage keys, their display
// order, and which RunStats fields represent each stage's input/output
// counts. Dashboard.tsx (live funnel) and Pipeline.tsx (historical funnel)
// both derive their stage rows from PIPELINE_STAGES instead of each keeping
// its own copy — see src/pipeline/orchestrator.py for the backend stage
// order this mirrors.
//
// `input`/`done` are typed as `(s: RunStats) => number | undefined` rather
// than a bare field-name string specifically so a typo or renamed RunStats
// field fails TypeScript compilation here, at the single source, instead of
// silently returning undefined at every call site.
// ---------------------------------------------------------------------------

export interface PipelineStageDef {
  key: string
  /** Noun-phrase title for a funnel row (e.g. "Requirements Gate"). */
  label: string
  /** Present-continuous phrase for the "currently running" status text and
   * activity log (e.g. "Checking requirements"). */
  activityLabel: string
  unit: string
  input: (s: RunStats) => number | undefined
  done: (s: RunStats) => number | undefined
}

export const PIPELINE_STAGES: PipelineStageDef[] = [
  {
    key: 'collect', label: 'Collection', activityLabel: 'Collecting jobs', unit: 'sources',
    input: () => undefined, done: s => s.collected,
  },
  {
    key: 'validate', label: 'Validation', activityLabel: 'Validating jobs', unit: 'jobs',
    input: s => s.collected, done: s => s.validated,
  },
  {
    key: 'deduplicate', label: 'Deduplication', activityLabel: 'Deduplicating jobs', unit: 'jobs',
    input: s => s.validated, done: s => s.deduplicated,
  },
  {
    key: 'filter', label: 'Hard Filter', activityLabel: 'Applying hard filters', unit: 'jobs',
    input: s => s.hard_filter_input, done: s => s.hard_filter_passed,
  },
  {
    key: 'requirements_gate', label: 'Requirements Gate', activityLabel: 'Checking requirements', unit: 'jobs',
    input: s => s.requirements_gate_input, done: s => s.requirements_gate_passed,
  },
  // Optional, AI-enabled-only gate (src/ai/eligibility.py) that runs after
  // the deterministic requirements gate and before scoring — see
  // EligibilityGate.run_stage. Always carries real (non-undefined) counts
  // on a completed run: the backend defaults llm_eligibility_input/passed to
  // the requirements-gate survivor count even when the stage is skipped
  // (disabled in config, or --ai not passed), the same "always populated"
  // convention hard_filter_*/requirements_gate_* already follow.
  {
    key: 'llm_eligibility', label: 'AI Eligibility Check', activityLabel: 'Checking eligibility (AI)', unit: 'jobs',
    input: s => s.llm_eligibility_input, done: s => s.llm_eligibility_passed,
  },
  {
    key: 'score', label: 'Scoring', activityLabel: 'Scoring jobs', unit: 'jobs',
    // what scoring receives: the jobs both eligibility gates passed on
    input: s => s.llm_eligibility_passed ?? s.requirements_gate_passed, done: s => s.scored,
  },
  {
    key: 'phase2', label: 'AI Understanding', activityLabel: 'Understanding jobs (AI)', unit: 'jobs',
    input: s => s.phase2?.total, done: s => s.phase2?.completed,
  },
  {
    key: 'semantic', label: 'Semantic Pre-Ranking', activityLabel: 'Semantic matching', unit: 'jobs',
    input: s => s.scored, done: s => s.semantically_matched,
  },
  {
    key: 'personalize', label: 'Personalized Ranking', activityLabel: 'Personalizing ranking', unit: 'jobs',
    input: s => s.semantically_matched, done: s => s.personalized,
  },
]

export function findPipelineStage(key: string): PipelineStageDef | undefined {
  return PIPELINE_STAGES.find(s => s.key === key)
}

// Activity-phrase labels for every stage, plus "preflight" — a preparation
// step (src/pipeline/preflight.py) that runs before collection and never
// produces a countable RunStats field, so it has no PIPELINE_STAGES entry
// (no funnel row) but still needs a label for the live status text.
const EXTRA_ACTIVITY_LABELS: Record<string, string> = {
  preflight: 'Preparing run',
}

export const STAGE_LABELS: Record<string, string> = {
  ...EXTRA_ACTIVITY_LABELS,
  ...Object.fromEntries(PIPELINE_STAGES.map(s => [s.key, s.activityLabel])),
}

export function stageLabel(stage: string): string {
  if (!stage) return 'Working'
  return STAGE_LABELS[stage] || stage
}
