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

// Distance model (F4.3, src/pipeline/location_distance.py): where the user lives.
export interface HomePlace {
  name: string
  insee: string | null
  lat: number
  lon: number
  department: string | null
  precision: 'commune' | 'department'
}

export interface RemotePolicy {
  remote_france: boolean
  remote_eu: boolean
  remote_worldwide: boolean
  hybrid: boolean
}

export interface Geography {
  home: HomePlace | null
  max_distance_km: number
  // The only hard location exclusion: on-site, beyond max_distance_km, and this ticked.
  strict_distance: boolean
  remote_policy: RemotePolicy
  // v1 "excluded" départements kept by the migration (no location credit there; not editable here yet).
  excluded_departments?: string[]
  preferred_countries: string[]
  allowed_regions: string[]
  // The SOLE source of truth for location_fit scoring's priority tiers —
  // a ranking preference, never a hard
  // filter. Empty by default; no location is preferred until the user adds
  // one on the Profile page.
  location_preferences: LocationPreference[]
}

export interface Experience {
  preferred_levels: string[]
  max_years: number | null
  excluded_seniority: string[]
}

// Profile v2: facts and career targets. The matching reads the target levels and held education levels.
export type CareerLevel = 'internship' | 'alternance' | 'graduate' | 'junior' | 'mid' | 'senior' | 'lead' | 'manager'

export interface ExperienceEntry {
  id: string
  role: string
  employer: string | null
  start: string // "YYYY-MM"
  end: string | null // null = ongoing
  skills: string[]
  responsibilities: string[]
  relevant: boolean
}

export interface EducationEntry {
  id: string
  qualification: string
  level: 3 | 4 | 5 | 6 | 7 | 8 | null // French national level = EQF level
  field: string | null
  institution: string | null
  year: number | null
  in_progress: boolean
  foreign: boolean
}

export interface CareerTargets {
  levels: CareerLevel[]
  stretch: boolean
  years_override: number | null
}

// The one-time "we updated your profile, please check" screen.
export interface ProfileReview {
  pending: boolean
  notes: string[]
  migrated_at: string | null
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
  profile_version?: number
  experiences?: ExperienceEntry[]
  education?: EducationEntry[]
  targets?: CareerTargets
  review?: ProfileReview | null
}

export interface SourceStats {
  collected: number
  accepted: number
  errors: number
  // completed | partial | failed | skipped; absent on runs recorded before it existed
  status?: string | null
  // why a skipped source was left out and what to set up
  reason?: string | null
  // no source answered at all in this run: the machine's connection, not the sources
  network_outage?: boolean | null
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
  // Phase 5: the canonical tier
  // (src.pipeline.scoring.score_to_tier) — never recomputed on the frontend.
  score_100: number | null
  tier: string | null
  // ADR 0019 v5 AD-2b (L5.2): weak hard-filter violations that ranked instead of rejecting.
  weak_flags?: WeakFlag[]
  weak_penalty?: number
}

export interface WeakFlag {
  code: string
  observed: string | null
  expected: string | null
  severity: number
}

export interface Provenance {
  source: string
  url: string | null
  first_seen_at: string
  last_seen_at: string
}

export interface Job {
  id: string
  // Expiry (issue #86): the source's end date; when and why the offer was marked expired.
  valid_through?: string | null
  expired_at?: string | null
  expiry_reason?: 'valid_through' | 'not_seen' | 'user_report' | null
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
  // Every source the offer was seen on, its own source first (issue #86; detail only).
  provenance?: Provenance[]
  // List management (ADR 0016, ADR 0017): the user saved / hides this offer.
  saved?: boolean
  hidden?: boolean
}

export interface Dashboard {
  latest_run: PipelineRun | null
  total_jobs_in_db: number
  is_running: boolean
  current_run_id: string | null
}
