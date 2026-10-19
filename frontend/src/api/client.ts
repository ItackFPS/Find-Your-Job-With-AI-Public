import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

// ---- Types ----------------------------------------------------------------

// Location Priority feature (2026-09-10). Array position IS priority (index
// 0 = strongest preference) — there is deliberately no separate numeric
// priority field, so reordering this array in the UI is reprioritizing.
// "excluded" vetoes only that one entry (see the resolver docs in
// src/pipeline/location_match.py) — it never makes location a hard filter
// elsewhere; a job matching no preference at all is still fully rankable.
export interface LocationPreference {
  type: 'department' | 'remote'
  // Department: French INSEE code (e.g. "83", "06", "69"). Remote: a
  // display id, e.g. "remote_france" (only one remote kind exists today).
  value: string
  status: 'preferred' | 'acceptable' | 'excluded'
  label?: string | null
}

export interface Geography {
  preferred_countries: string[]
  allowed_regions: string[]
  // The SOLE source of truth for location_fit scoring's priority tiers and
  // for Discord notification routing — a ranking preference, never a hard
  // filter. Empty by default; no location is preferred until the user adds
  // one on the Profile page.
  location_preferences: LocationPreference[]
}

export interface Experience {
  preferred_levels: string[]
  max_years: number | null
  excluded_seniority: string[]
}

export interface EscoEntry {
  concept_uri: string | null
  label: string
  keyword: string
  custom: boolean
}

export interface EscoSuggestion {
  concept_uri: string
  label: string
  label_fr: string
  keyword: string
  alt_labels_en: string[]
  alt_labels_fr: string[]
  concept_type: string
  is_esco: boolean
}

export interface Matching {
  education_hard_constraint: boolean
  education_preferred_penalty: number
  principal_skill_gate: boolean
  mandatory_central_skill_gate: boolean
  llm_extraction_enabled: boolean
}

export interface Profile {
  target_domains: string[]
  focus_domain: boolean
  target_skills: string[]
  target_skills_esco?: EscoEntry[]
  geography: Geography
  work_modes: string[]
  contract_types: string[]
  experience: Experience
  languages: string[]
  freshness_days: number
  max_education: string | null
  matching: Matching
  // Free-text description of the desired role, in the user's own words.
  // Stored only — not yet read by any matching logic (a future Phase 2/3
  // semantic-matching input).
  looking_for?: string | null
}

export interface SourceStats {
  collected: number
  accepted: number
  errors: number
}

export interface RunStats {
  collected: number
  validated: number
  deduplicated: number
  filtered: number
  // Absent on runs recorded before this breakdown was added.
  hard_filter_input?: number
  hard_filter_passed?: number
  requirements_gate_input?: number
  requirements_gate_passed?: number
  // Optional LLM semantic eligibility gate (src/ai/eligibility.py) — runs
  // after the requirements gate, before scoring, only when --ai is passed
  // and matching.llm_eligibility_enabled is set. Absent on runs recorded
  // before this stage existed; llm_eligibility_input/_passed are otherwise
  // always present (defaulted to a pass-through count when the stage is
  // skipped), mirroring hard_filter_*/requirements_gate_* above.
  llm_eligibility_input?: number
  llm_eligibility_passed?: number
  llm_eligibility_rejected?: number
  llm_eligibility?: {
    successful?: number
    failed?: number
    uncertain?: number
    passed?: number
    rejected?: number
    unsupported_reject?: number
    cached?: number
    time_seconds?: number
    error?: string
  }
  scored: number
  semantically_matched: number
  personalized: number
  duration_seconds: number
  by_source: Record<string, SourceStats>
  by_country: Record<string, number>
  by_work_mode: Record<string, number>
  errors: string[]
  profile_hash: string
  model_name: string
  scoring_config_hash: string
  current_stage: string
  // Whether this run was started with Phase 2 (local AI) enabled.
  use_ai?: boolean
  // Live Phase 2 progress/result summary — see PipelineStats.phase2.
  // Absent/empty when AI is disabled or Phase 2 hasn't started yet.
  phase2?: {
    total?: number
    completed?: number
    failed?: number
    percent?: number
    elapsed_seconds?: number
    successful?: number
    skipped?: number
    cached?: number
    retried?: number
    error?: string
  }
  // Live item-level progress for whichever stage is currently running —
  // see PipelineStats.stage_progress. Empty between updates/when idle.
  stage_progress?: {
    stage?: string
    processed?: number
    total?: number
  }
}

export interface PipelineRun {
  id: string
  started_at: string
  completed_at: string | null
  status: 'running' | 'completed' | 'failed'
  stats: RunStats
  error: string | null
}

export interface ScoreBreakdown {
  deterministic_score: number | null
  semantic_score: number | null
  final_score: number | null
  factors: Record<string, number>
  run_id: string | null
  // Phase 5: same canonical tier every notification channel already uses
  // (src.pipeline.scoring.score_to_tier) — never recomputed on the frontend.
  score_100: number | null
  tier: string | null
}

export interface Job {
  id: string
  source: string
  title: string | null
  company: string | null
  location: string | null
  // Phase 5: resolved French department code (see JobOut in
  // src/api/schemas.py) — lets the frontend label Var/PACA without its own
  // location-parsing logic.
  department: string | null
  countries: string[]
  remote: boolean
  work_mode: string | null
  employment_type: string | null
  salary_min: number | null
  salary_max: number | null
  salary_currency: string
  published_at: string | null
  discovered_at: string
  skills: string[]
  canonical_url: string | null
  apply_url: string | null
  status: string
  application_status: string | null
  score: ScoreBreakdown | null
  description?: string | null
}

export interface Dashboard {
  latest_run: PipelineRun | null
  total_jobs_in_db: number
  is_running: boolean
  current_run_id: string | null
}

// ---- Live pipeline progress (SSE) ------------------------------------------

// Mirrors src/api/schemas.py PipelineEventOut — the payload streamed by
// GET /api/pipeline/events. `stats` reuses the same RunStats shape returned
// by GET /pipeline/runs/{id}; this is a live view of that same data, not a
// second representation of pipeline state.
export interface PipelineEvent {
  type: 'pipeline.started' | 'pipeline.progress' | 'pipeline.completed' | 'pipeline.failed' | 'pipeline.snapshot'
  run_id: string
  status: 'running' | 'completed' | 'failed'
  stage: string
  stats: RunStats
  timestamp: string
  error: string | null
}

export const PIPELINE_EVENTS_URL = '/api/pipeline/events'

export interface ExplanationReason {
  emoji: string
  text: string
  kind: 'positive' | 'concern' | 'rejection'
}

export interface JobExplanation {
  job_id: string
  overall_score: number | null
  score: ScoreBreakdown | null   // only `factors` is rendered (Match factors); the rest stays internal
  filter_result: string
  rejection_reason: string | null
  positive_reasons: ExplanationReason[]
  concerns: ExplanationReason[]
  rejection_reasons: ExplanationReason[]
  // 'decision' = rendered from the decision journal; 'heuristic' = legacy analysis (job predates the journal)
  explanation_source?: 'decision' | 'heuristic'
  decision_id?: string | null
  personalization_signals: {
    signal_type: string
    value: string
    contribution: number
    feedback_count: number
  }[]
  personalization_adjustment: number
}

// ---- API calls ------------------------------------------------------------

export const getProfile = () => api.get<Profile>('/profile').then(r => r.data)
export const updateProfile = (p: Profile) => api.put<Profile>('/profile', p).then(r => r.data)

export const searchEscoSkills = (q: string, lang: 'en' | 'fr' = 'en', limit = 8): Promise<EscoSuggestion[]> =>
  api.get<EscoSuggestion[]>('/esco/skills', { params: { q, lang, limit } }).then(r => r.data)

export const searchEscoOccupations = (q: string, lang: 'en' | 'fr' = 'en', limit = 8): Promise<EscoSuggestion[]> =>
  api.get<EscoSuggestion[]>('/esco/occupations', { params: { q, lang, limit } }).then(r => r.data)

export interface FrenchDepartment {
  code: string   // e.g. "83", "2A", "971"
  name: string   // e.g. "Var", "Corse-du-Sud"
}

export const getAllDepartments = (): Promise<FrenchDepartment[]> =>
  api.get<FrenchDepartment[]>('/departments/all').then(r => r.data)

export const searchFrenchDepartments = (q: string, limit = 8): Promise<FrenchDepartment[]> =>
  api.get<FrenchDepartment[]>('/departments', { params: { q, limit } }).then(r => r.data)

export interface CountryFacet { name: string; count: number }
export interface CountriesResponse { total: number; facets: CountryFacet[] }

export const getJobCountries = (): Promise<CountriesResponse> =>
  api.get<CountriesResponse>('/jobs/countries').then(r => r.data)

// ---- Stats page ------------------------------------------------------------
// Mirrors src/api/routes/jobs.py::JobStatsOut — real, DB-derived facets
// scoped to the latest completed run (same scoping as /jobs/countries).

export interface SkillFacet { name: string; count: number }
export interface ScoreTierFacet { tier: string; count: number }
export interface RejectionFacet { stage: string; detail: string; count: number }
export interface TitleCategoryFacet { name: string; count: number }

export interface JobStats {
  total_jobs: number
  skills: SkillFacet[]
  tools: SkillFacet[]
  score_tiers: ScoreTierFacet[]
  rejections: RejectionFacet[]
  title_categories: TitleCategoryFacet[]
}

export const getJobStats = (): Promise<JobStats> =>
  api.get<JobStats>('/jobs/stats').then(r => r.data)

// ---- Notification provider status (sidebar footer) -------------------------

export interface NotificationProviderStatus {
  enabled: boolean
  configured: boolean
  last_sent: string | null
  // n8n only: actual server reachability right now (via n8n's own REST API),
  // independent of `configured` (which only means a push-webhook URL is set).
  reachable?: boolean
}

export interface NotificationStatus {
  enabled: boolean
  providers: Record<string, NotificationProviderStatus>
  counts: Record<string, number>
  // Built from DISCORD_GUILD_ID on the backend; null when it is not set.
  discord_server_url?: string | null
}

export const getNotificationStatus = (): Promise<NotificationStatus> =>
  api.get<NotificationStatus>('/pipeline/notifications/status').then(r => r.data)

export const getDashboard = () => api.get<Dashboard>('/dashboard').then(r => r.data)

export const startRun = (useAi = false) =>
  api.post<{ run_id: string; status: string }>('/pipeline/run', { use_ai: useAi }).then(r => r.data)

export const getRun = (id: string) => api.get<PipelineRun>(`/pipeline/runs/${id}`).then(r => r.data)
export const listRuns = (limit = 20) =>
  api.get<PipelineRun[]>(`/pipeline/runs?limit=${limit}`).then(r => r.data)

export const getRunJobs = (runId: string, limit = 100, offset = 0) =>
  api.get<Job[]>(`/pipeline/runs/${runId}/jobs?limit=${limit}&offset=${offset}`).then(r => r.data)

export const listJobs = (params?: {
  source?: string
  country?: string
  department?: string
  remote?: boolean
  q?: string
  // Phase 5: presentation-only visibility floor (never deletes/filters
  // storage) — 'exceptional' | 'excellent' | 'strong' | 'relevant' |
  // 'low_priority'. Omit to see every scored job.
  min_tier?: string
  limit?: number
  offset?: number
}) => api.get<Job[]>('/jobs', { params }).then(r => r.data)

export const getJob = (id: string) => api.get<Job>(`/jobs/${id}`).then(r => r.data)
export const getJobExplanation = (id: string) =>
  api.get<JobExplanation>(`/jobs/${id}/explanation`).then(r => r.data)
export const updateJobApplicationStatus = (id: string, status: string | null) =>
  api.patch<Job>(`/jobs/${id}/application-status`, { application_status: status }).then(r => r.data)

// ---- Decision journal (read-only; see docs/adr/0008-decision-journal.md) ----
export interface DecisionEvidence {
  dimension: string
  polarity: 'positive' | 'negative' | 'unknown'
  state: 'satisfied' | 'violated' | 'unknown'
  message: string
  observed: string | null
  expected: string | null
  weight: number | null
  source: { stage: string; rule_id: string; rule_version: string | null; evidence_source: string | null }
  span: { field: string; start: number; end: number; text: string; extractor_id: string } | null
}

export interface DecisionAssessment {
  state: 'accept' | 'review' | 'reject'
  confidence: number
  reasons: string[]
  unknowns: string[]
  mode: 'shadow' | 'active'
  calibrated: boolean
}

// A rule-backed rejection picked by the rejection audit sample (src/decisions/audit.py)
export interface DecisionAudit {
  sampled: boolean
  sampler: string
  stratum: string
  stratum_size: number
  rank_in_stratum: number
  max_open?: number
}

export interface DecisionRecord {
  decision_id: string
  run_id: string
  job_id: string
  decided_at: string
  outcome: 'accepted' | 'rejected' | 'review'
  deciding_stage: string
  strategy_version: string
  fit_score: number | null
  // agreement-based, NOT a verbalised LLM confidence; uncalibrated until calibrated on labelled data
  decision_confidence?: number | null
  confidence_calibrated?: boolean
  signals?: { assessment?: DecisionAssessment; audit?: DecisionAudit } & Record<string, unknown>
  evidence: DecisionEvidence[]
  conflicts: { dimensions: string[]; description: string }[]
  stage_trace: { stage: string; result: string; detail: string | null }[]
  exposure: { rank: number | null; tier: string | null; final_score: number | null } | null
  summary: { supporting: number; against: number; unknown: number; conflicts: number; headline: string }
  job?: { title: string | null; company: string | null; source: string | null; location: string | null }
}

// 404 = no decision recorded for this job (older than the journal): not an error for the UI.
export const getJobDecision = (id: string) =>
  api.get<DecisionRecord>(`/jobs/${id}/decision`).then(r => r.data).catch(err => {
    if (err?.response?.status === 404) return null
    throw err
  })

// Review queue: latest decisions the disagreement engine flagged `review` that the user has not answered
// yet — rejected jobs included, so a doubtful rejection is never silent (docs/adr/0010).
export interface ReviewItem {
  decision: DecisionRecord
  job_stored: boolean
  has_snapshot: boolean
}
export interface ReviewQueuePage { total: number; limit: number; offset: number; items: ReviewItem[] }
export const getReviewQueue = (outcome?: 'accepted' | 'rejected') =>
  api.get<ReviewQueuePage>('/decisions/review-queue', { params: outcome ? { outcome } : {} }).then(r => r.data)
export interface DecisionSnapshot { title?: string | null; company?: string | null; location?: string | null; description?: string | null }
export const getDecisionSnapshot = (decisionId: string) =>
  api.get<DecisionSnapshot>(`/decisions/${decisionId}/snapshot`).then(r => r.data)
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

// ---- Feedback journal (append-only; see docs/adr/0009-feedback-journal.md) ----
export interface FeedbackEvent {
  event_id: string
  job_id: string
  decision_id: string | null
  strategy_version: string | null
  signal_class: 'explicit' | 'behavior' | 'outcome'
  type: string
  reasons: string[]
  note: string | null
  source: string
  occurred_at: string
  metadata: Record<string, unknown>
}

export interface FeedbackVocabulary {
  explicit_types: string[]
  reasons: string[]
  allowed_reasons: Record<string, string[]>
  canonical_statuses: string[]
}

export const getFeedbackVocabulary = () =>
  api.get<FeedbackVocabulary>('/jobs/feedback/vocabulary').then(r => r.data)
export const getJobFeedback = (id: string) =>
  api.get<FeedbackEvent[]>(`/jobs/${id}/feedback`).then(r => r.data)
export const postJobFeedback = (
  id: string,
  body: { type: string; reasons: string[]; note?: string; decision_id?: string },
) => api.post<FeedbackEvent>(`/jobs/${id}/feedback`, { ...body, source: 'ui' }).then(r => r.data)

// I18 outcome tracking: fire-and-forget, best-effort. Only "viewed" and
// "apply_link_opened" may be reported this way — never let a failure here
// affect navigation or the surrounding UI.
export const recordJobEvent = (id: string, eventType: 'viewed' | 'apply_link_opened') =>
  api.post(`/jobs/${id}/events`, { event_type: eventType }).catch(() => undefined)

// ---- Scan pause / resume (issue #81) ---------------------------------------
// Shared by the LinkedIn and HelloWork scanners. `state` comes from the
// backend's persisted checkpoint: 'paused' survives restarts until resumed,
// 'interrupted' (shutdown/crash) is resumed automatically by the scheduler.

export type ScanState = 'idle' | 'running' | 'pausing' | 'paused' | 'interrupted'

export interface ScanResumed {
  scan_id: string | null
  exec_id: string | null
}

// ---- LinkedIn ------------------------------------------------------------

export interface LinkedInScanQueryStats {
  search_term: string
  location: string
  pages_fetched: number
  jobs_raw: number
  jobs_unique: number
  jobs_filtered_out: number
  jobs_already_known: number
  jobs_staged: number
  stopped_by: string
}

export interface LinkedInScanStats {
  total_raw: number
  total_unique: number
  total_cross_query_dups: number
  total_filtered_out: number
  total_already_known: number
  total_staged: number
  total_pages_fetched: number
  queries_completed: number
  queries_stopped_by_exhaustion: number
  queries_stopped_by_limit: number
  queries_failed: number
  queries: LinkedInScanQueryStats[]
}

export interface LinkedInScanRun {
  id: string
  started_at: string
  completed_at: string | null
  status: 'running' | 'completed' | 'failed' | 'paused' | 'interrupted'
  jobs_found: number
  jobs_new: number
  error: string | null
  stats: LinkedInScanStats | null
}

// Durable checkpoint progress; current_page is the next page to fetch (0-based).
// The *_est / percent / eta_* fields are the backend's work-based estimate
// (issue #86): page-weighted, stable across restarts; eta_seconds is null
// unless the scan is actively running and enough throughput data exists.
export interface LinkedInScanProgress {
  queries_completed: number
  queries_total: number
  current_query: string | null
  current_page: number
  updated_at: string | null
  percent?: number
  pages_done_est?: number
  pages_total_est?: number
  current_query_pages_est?: number | null
  estimated?: boolean
  eta_seconds?: number | null
  eta_basis?: 'live' | 'checkpoint' | 'history' | null
}

export interface LinkedInStatus {
  enabled: boolean
  waiting: number
  last_scan: LinkedInScanRun | null
  is_scanning: boolean
  state?: ScanState
  progress?: LinkedInScanProgress | null
}

export const getLinkedInStatus = () =>
  api.get<LinkedInStatus>('/linkedin/status').then(r => r.data)

export const triggerLinkedInScan = () =>
  api.post<{ scan_id: string }>('/linkedin/scan').then(r => r.data)

export const pauseLinkedInScan = () =>
  api.post<LinkedInStatus>('/linkedin/pause').then(r => r.data)

export const resumeLinkedInScan = () =>
  api.post<ScanResumed>('/linkedin/resume').then(r => r.data)

export const listLinkedInScans = (limit = 10) =>
  api.get<LinkedInScanRun[]>(`/linkedin/scans?limit=${limit}`).then(r => r.data)

// ---- HelloWork -------------------------------------------------------------
// Runs decoupled from the main pipeline (like LinkedIn above) because a full
// scan takes ~30 minutes — see src/hellowork/scanner.py.

export interface HelloWorkScanStats {
  collector_errors: number
  collection_duration_seconds: number
  metier_slugs_queried: number | null
}

export interface HelloWorkScanRun {
  id: string
  started_at: string
  completed_at: string | null
  status: 'running' | 'completed' | 'failed' | 'paused' | 'interrupted'
  jobs_found: number
  jobs_new: number
  error: string | null
  stats: HelloWorkScanStats | null
}

// The slug total is only known once the scan resolves it from the network,
// so progress is a count, never a percentage.
export interface HelloWorkScanProgress {
  slugs_completed: number
  updated_at: string | null
}

export interface HelloWorkStatus {
  enabled: boolean
  waiting: number
  last_scan: HelloWorkScanRun | null
  is_scanning: boolean
  state?: ScanState
  progress?: HelloWorkScanProgress | null
}

export const getHelloWorkStatus = () =>
  api.get<HelloWorkStatus>('/hellowork/status').then(r => r.data)

export const triggerHelloWorkScan = () =>
  api.post<{ scan_id: string }>('/hellowork/scan').then(r => r.data)

export const pauseHelloWorkScan = () =>
  api.post<HelloWorkStatus>('/hellowork/pause').then(r => r.data)

export const resumeHelloWorkScan = () =>
  api.post<ScanResumed>('/hellowork/resume').then(r => r.data)

export const listHelloWorkScans = (limit = 10) =>
  api.get<HelloWorkScanRun[]>(`/hellowork/scans?limit=${limit}`).then(r => r.data)
