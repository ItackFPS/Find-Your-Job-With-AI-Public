import { api } from './http'
import type { Dashboard, EscoSuggestion, Job, PipelineRun, Profile, ProfileReview, RunStats, ScoreBreakdown } from './types'

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
// Saves only the review flag (the PUT merges), so confirming never saves unsaved edits.
export const confirmProfileReview = (review: ProfileReview) =>
  api.put<Profile>('/profile', { review: { ...review, pending: false } }).then(r => r.data)

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

export interface PlaceSuggestion {
  name: string
  insee: string
  department: string | null
  department_name: string | null
  lat: number
  lon: number
}

// Offline commune autocomplete (bundled table; nothing leaves the machine).
export const searchPlaces = (q: string, limit = 8): Promise<PlaceSuggestion[]> =>
  api.get<PlaceSuggestion[]>('/geo/places', { params: { q, limit } }).then(r => r.data)

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

// ---- Backend health (sidebar footer) -------------------------------------

export interface Health {
  status: string
  version?: string
}

export const getHealth = (): Promise<Health> =>
  api.get<Health>('/health').then(r => r.data)

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
  // Also list offers marked expired (issue #86); hidden by default.
  include_expired?: boolean
  // Hide (ADR 0017): hidden offers are left out unless asked for; saved_only lists saved offers.
  include_hidden?: boolean
  saved_only?: boolean
  limit?: number
  offset?: number
}) => api.get<Job[]>('/jobs', { params }).then(r => r.data)

// Découverte (S4; ADR 0019): the latest run's random slots, apart from the main list.
export const listDecouverte = () => api.get<Job[]>('/jobs/decouverte').then(r => r.data)
export const listOutsideCriteria = () => api.get<Job[]>('/jobs/outside-criteria').then(r => r.data)

// The user reports an offer as no longer available, or available after all (issue #86).
export const setJobAvailability = (id: string, available: boolean) =>
  api.post<Job>(`/jobs/${id}/availability`, { available }).then(r => r.data)

export const getJob = (id: string) => api.get<Job>(`/jobs/${id}`).then(r => r.data)
export const getJobExplanation = (id: string) =>
  api.get<JobExplanation>(`/jobs/${id}/explanation`).then(r => r.data)
export const updateJobApplicationStatus = (id: string, status: string | null) =>
  api.patch<Job>(`/jobs/${id}/application-status`, { application_status: status }).then(r => r.data)

// ---- Scan pause / resume (issue #81) ---------------------------------------
// Shared by the LinkedIn and HelloWork scanners. `state` comes from the
// backend's persisted checkpoint: 'paused' survives restarts until resumed,
// 'interrupted' (shutdown/crash) is resumed automatically by the scheduler.

// 'rate_limited': stopped on the source's 429 (F6.4); resumed by the scheduler after its cooldown.
export type ScanState = 'idle' | 'running' | 'pausing' | 'paused' | 'interrupted' | 'rate_limited'

export interface ScanResumed {
  scan_id: string | null
  exec_id: string | null
}
