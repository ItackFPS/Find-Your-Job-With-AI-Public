import { api } from './http'

// ---- Career Skill Graph (read-only; src/skillgraph/graph.py) ----
export interface SkillNode { skill: string; demand: number; relevant_demand: number; relevant_share: number; owned: boolean }
export interface SkillEdge { source: string; target: string; weight: number }
export interface SkillGap {
  skill: string; relevant_demand: number; demand: number; relevant_share: number
  adjacent_owned: { skill: string; jobs: number }[]; example_titles: string[]; why: string
}
export interface SkillGraph {
  window_days: number; relevant_definition: string; n_jobs: number; n_relevant: number
  nodes: SkillNode[]; edges: SkillEdge[]; gaps: SkillGap[]; owned: string[]; limitations: string[]
}
export const getSkillGraph = (days = 90) =>
  api.get<SkillGraph>('/skills/graph', { params: { days } }).then(r => r.data)
// ---- Evaluation runs and experiment lab (read-only; docs/adr/0007, 0011) ----
export interface RunMetrics { tp: number | null; tn: number | null; fp: number | null; fn: number | null; borderline: number | null
  accuracy: number | null; precision: number | null; recall: number | null; unsafe_rejection_rate: number | null }
export interface EvalRunSummary {
  file: string; run_id: string; created_at: string
  dataset: { id: string; version: string; kind: string; jobs_sha256: string; labels_sha256: string }
  strategy: { name: string; fingerprint: string }; n_labelled: number | null; overall: RunMetrics
  review_status: string | null; warnings: string[]
}
export interface LabExperiment {
  experiment_id: string; created_at: string; candidate_id: string; dataset_id: string; verdict: string
  eligible_for_approval: boolean; candidate_status: string | null; hypothesis: string | null
  // what supports an eligible experiment: only `independently_supported` rests on independent human labels
  promotion_evidence: 'independently_supported' | 'exploratory_only' | 'not_eligible'
  independently_validated: boolean; evidence_basis: string | null
  overrides: Record<string, unknown>; explanation: string | null; fixed: number | null; broken: number | null
  guardrails: Record<string, boolean>; delta: Record<string, number | null> | null; limitations: string[]
  // what the evidence recommends a human do; absent from backends that predate it
  next_action?: 'approved' | 'submit_for_human_approval' | 'reject' | 'investigate'
}
// Self-check report (src/evaluation/selfcheck.py): the current strategy vs the recorded baseline.
export type SelfCheckStatus = 'safety_violation' | 'baseline_mismatch' | 'regression' | 'changed' | 'pass'
export interface SelfCheckPattern {
  kind: 'unsafe_rejection' | 'false_positive' | 'borderline_rejected_without_review'; key: string; count: number
  job_ids: string[]; example_reasons: string[]; lever: { kind: 'lab' | 'code'; detail: string }
}
export interface SelfCheckDataset {
  status: SelfCheckStatus
  dataset: { id: string; version: string; kind: string; n_jobs: number; n_labelled: number; accept_labels: number
             label_provenance: Record<string, number> }
  strategy: { name: string | null; fingerprint: string | null }
  safety: { unsafe_rejections: { job_id: string; reasons: string[] }[]; rejections_without_reason: string[] }
  eligibility: Record<string, number | null>
  explanations: { rejections: number; with_reason: number; coverage: number | null }
  ranking: { status: string; why: string; score_separation: { status: string; pairwise_auc?: number } }
  baseline: { status: 'identical' | 'differs' | 'mismatch' | 'missing'
              flips?: { fixed: string[]; broken: string[]; unjudged: string[] }; delta?: Record<string, number | null> }
  patterns: SelfCheckPattern[]; limitations: string[]
}
export interface SelfCheckReport {
  created_at: string; status: SelfCheckStatus; generated_by: string; datasets: SelfCheckDataset[]
  recommendations: { kind: 'action' | 'suggestion'; dataset: string; text: string }[]; next_steps: string
  feedback?: SelfCheckFeedback
}
// What explicit feedback says about the latest decisions (selfcheck.feedback_coverage): user signals, not labels.
export type SelfCheckFeedback =
  | { status: 'unavailable'; why: string }
  | { status: 'measured'; decided_jobs: number; jobs_with_explicit_feedback: number; coverage: number | null
      latest_answers: Record<string, number>; decisions_with_evidence: number; explanation_coverage: number | null
      contradictions: Record<'rejected_but_interested' | 'accepted_but_not_interested', { count: number; job_ids: string[] }>
      note: string }
export interface SelfCheckResponse {
  latest: SelfCheckReport | null; unreadable: number
  history: { file: string; created_at: string; status: SelfCheckStatus | null; datasets: Record<string, string> }[]
}
export interface LabMemoryItem { item_id: string; created_at: string; kind: string; statement: string; status: string | null }
// What the pipeline actually runs (src/decisions/active_strategy.py): `active` = the approved bundle; any other
// status (default | deactivated | inert | invalid | stale) = config/.
export interface PipelineStrategy {
  status: 'default' | 'deactivated' | 'inert' | 'invalid' | 'stale' | 'active'; reason: string
  pointer_id: string | null; bundle_hash: string | null; action: string | null; approved_by: string | null
  // what the latest approval rested on; `unrecorded`: approved without recorded evidence (never "validated")
  approval_basis: 'independently_supported' | 'exploratory_only' | 'unrecorded' | null
}
export interface ActiveStrategy {
  current: Record<string, string> | null; history: Record<string, string>[]; honoured_by_pipeline: boolean
  pipeline: PipelineStrategy; note: string
}
export const getEvalRuns = () => api.get<{ total: number; unreadable: number; runs: EvalRunSummary[] }>('/lab/runs').then(r => r.data)
export const getLabExperiments = () => api.get<{ experiments: LabExperiment[] }>('/lab/experiments').then(r => r.data)
export const getLabMemory = () => api.get<{ items: LabMemoryItem[] }>('/lab/memory').then(r => r.data)
export const getActiveStrategy = () => api.get<ActiveStrategy>('/lab/active-strategy').then(r => r.data)
export const getSelfCheck = () => api.get<SelfCheckResponse>('/lab/self-check').then(r => r.data)
