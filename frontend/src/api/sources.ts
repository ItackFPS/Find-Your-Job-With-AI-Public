import { api } from './http'
import type { ScanResumed, ScanState } from './core'

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
  // When a rate-limited scan resumes by itself (state 'rate_limited', F6.4).
  resume_after?: string | null
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
  /** First few error messages, so the reason is visible, not just the count (older scans: absent). */
  collector_error_samples?: string[]
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
  // When a rate-limited scan resumes by itself (state 'rate_limited', F6.4).
  resume_after?: string | null
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

// ---- Settings › Sources and background (issue #82) -------------------------

// data/settings.json "background": every automatic run on/off, and the local time each
// day's runs become due (src/scheduler/service.py, issue #80).
export interface BackgroundSettings {
  enabled: boolean
  daily_time: string // "HH:MM", local time
}

export interface ScheduleExecution {
  id: string
  trigger: string
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'skipped' | string
  started_at: string
  finished_at: string | null
  error: string | null
}

export interface ScheduleStatus {
  id: string
  source_id: string
  label: string
  enabled: boolean
  startup_policy: string
  daily: boolean // runs on its own every day
  due: boolean // today's run is still to come
  next_run_at: string | null
  is_running: boolean
  last_execution: ScheduleExecution | null
}

export interface SchedulerStatus {
  schedules: ScheduleStatus[]
  paused: boolean
  background: BackgroundSettings | null
}

export interface SourceBreaker {
  source: string
  consecutive_failures: number
  paused: boolean
  paused_until: string | null
  last_failure_reason: string | null
}

export interface SourceOverview {
  name: string
  access_class: 'official' | 'public_page' | 'unofficial' | null
  available: boolean
  collected: boolean
  choice: { enabled: boolean; chosen_at: string | null } | null
  last_run: {
    started_at: string | null
    status: string | null
    reason: string | null
    collected: number | null
    accepted: number | null
    errors: number | null
  } | null
  breaker: SourceBreaker | null
}

export interface EmbeddingModelStatus {
  state: 'missing' | 'downloading' | 'ready' | 'failed'
  total_bytes: number
  downloaded_bytes: number
  error: 'offline' | 'disk_full' | 'corrupt' | 'failed' | null
}
export const getEmbeddingModel = () =>
  api.get<EmbeddingModelStatus>('/embedding-model').then(r => r.data)
export const startEmbeddingModelDownload = () =>
  api.post<EmbeddingModelStatus>('/embedding-model/download').then(r => r.data)
export interface OnboardingState {
  started: boolean
  step: number
  completed: boolean
  profile_saved: boolean
}
export const getOnboarding = () =>
  api.get<OnboardingState>('/settings/onboarding').then(r => r.data)
export const updateOnboarding = (changes: Partial<Pick<OnboardingState, 'step' | 'completed'>>) =>
  api.put<OnboardingState>('/settings/onboarding', changes).then(r => r.data)
export const getBackgroundSettings = () =>
  api.get<BackgroundSettings>('/settings/background').then(r => r.data)
export const updateBackgroundSettings = (changes: Partial<BackgroundSettings>) =>
  api.put<BackgroundSettings>('/settings/background', changes).then(r => r.data)
export interface AutostartSettings {
  supported: boolean
  enabled: boolean
}
export const getAutostart = () =>
  api.get<AutostartSettings>('/settings/autostart').then(r => r.data)
export const updateAutostart = (enabled: boolean) =>
  api.put<AutostartSettings>('/settings/autostart', { enabled }).then(r => r.data)
export const getSchedulerStatus = () =>
  api.get<SchedulerStatus>('/scheduler/status').then(r => r.data)
export const runScheduleNow = (id: string) =>
  api.post<{ exec_id: string }>(`/scheduler/run/${id}`).then(r => r.data)
export const getSourcesOverview = () =>
  api.get<{ sources: SourceOverview[] }>('/sources/overview').then(r => r.data)
export const setSourceChoice = (name: string, enabled: boolean) =>
  api.put<{ enabled: boolean; chosen_at: string }>(`/sources/${name}/choice`, { enabled }).then(r => r.data)
export const resetSourceBreaker = (name: string) =>
  api.post<SourceBreaker>(`/sources/${name}/breaker/reset`).then(r => r.data)

// Settings › Data (issue #86): database size and removing old expired offers.
export interface DataSettings {
  database_bytes: number
  offers: { active: number; expired: number }
  removable_expired: Record<'30' | '90' | '180', number>
  backups: { name: string; bytes: number }[]
  cleanup: {
    state: 'idle' | 'running' | 'done' | 'failed'
    removed?: number
    bytes_before?: number
    bytes_after?: number
    backup?: string | null
    compacted?: boolean
    error?: string
  }
}

export const getDataSettings = () => api.get<DataSettings>('/settings/data').then(r => r.data)
export const startDataCleanup = (olderThanDays: number) =>
  api.post<DataSettings['cleanup']>('/settings/data/cleanup', { older_than_days: olderThanDays }).then(r => r.data)
