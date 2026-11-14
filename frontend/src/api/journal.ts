import { setGenerationToken } from './generation'
import type { OutboxEntry, SendResult } from './outbox'
import { api } from './http'

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

// ---- Ordered delivery and visible exposure (ADR 0015, ADR 0016) ----
export interface SessionState {
  data_epoch: string
  erasure_generation: number
  token: string
  learning_paused: boolean
  learning_reset_at: string | null
}

export interface LearningState { paused: boolean; reset_at: string | null }
export const getLearningState = () => api.get<LearningState>('/privacy/learning').then(r => r.data)
export const setLearningPaused = (paused: boolean) =>
  api.put<LearningState>('/privacy/learning/pause', { paused }).then(r => r.data)
export const resetLearning = () =>
  api.post<LearningState & { cleared: Record<string, number> }>('/privacy/learning/reset', { confirm: true }).then(r => r.data)
// Personal ranking state (ADR 0019, v5 L4.4). `strata` is for the Quality page only.
export interface LearningStatus {
  state: 'shadow' | 'experimental_unconfirmed' | 'active' | 'paused'
  wording_key: string
  activation_enabled: boolean
  paused_notice: boolean
  state_since: string | null
  last_event: string | null
  strata: Record<string, { discordant: number; challenger_wins: number }>
}
export const getLearningStatus = () => api.get<LearningStatus>('/privacy/learning/status').then(r => r.data)
export const resumeLearning = () => api.post<LearningStatus>('/privacy/learning/resume').then(r => r.data)
// Offline learning curve (v5 L3.5): counts only. Runs in a child process, so it is fetched on demand.
export interface LearningCurve {
  status: 'no_positives' | 'no_anchored_positives' | 'measured'
  positives: number
  anchored: number
  discordant: number
  challenger_wins: number
  production_wins: number
  wording: { key: string; n: number; x: number }
}
export const getLearningCurve = () => api.get<LearningCurve>('/quality/learning-curve').then(r => r.data)

/** Erase my activity (ADR 0015): the latest erasure job; counts and checks only, never content. */
export interface ErasureStatus {
  state: string | null
  pending: boolean
  running: boolean
  erasure_id?: string
  requested_at?: string
  completed_at?: string | null
  last_state?: string
  last_error?: string | null
  counts?: Record<string, number> | null
  verification?: Record<string, boolean> | null
  lab_present?: boolean
}
export const getErasure = () => api.get<ErasureStatus>('/privacy/erase').then(r => r.data)
export const eraseActivity = ({ purge_lab, ...opts }: { keep_tracker: boolean; erase_calibration: boolean; purge_lab?: boolean }) =>
  api.post<ErasureStatus>('/privacy/erase', {
    confirm: true, ...opts, ...(purge_lab ? { purge_lab: true, confirm_lab: true } : {}),
  }).then(r => r.data)
export const retryErasure = () => api.post<ErasureStatus>('/privacy/erase/retry').then(r => r.data)

/** Delete all application data (ADR 0015): state read from a file outside the data folder. */
export interface DeleteAllStatus {
  state: string | null
  pending: boolean
  running: boolean
  restart_required: boolean
  last_state?: string | null
  last_error?: string | null
  requested_at?: string | null
  completed_at?: string | null
  skipped?: string[] | null
  // the backend is about to exit so its launcher restarts it (Windows locked files)
  restarting?: boolean
  auto_restarts?: number | null
}
export const getDeleteAll = () => api.get<DeleteAllStatus>('/privacy/delete-all').then(r => r.data)
export const deleteAllData = () =>
  api.post<DeleteAllStatus>('/privacy/delete-all', { confirm: true }).then(r => r.data)
export const retryDeleteAll = () => api.post<DeleteAllStatus>('/privacy/delete-all/retry').then(r => r.data)

export const getSession = () => api.get<SessionState>('/session').then(r => {
  setGenerationToken(r.data.token)
  return r.data
})

/** Outbox transport: one durable action, its HTTP status returned rather than thrown. */
export const sendOutboxEntry = (e: OutboxEntry): Promise<SendResult> =>
  api.post(`/jobs/${encodeURIComponent(e.job_id)}/feedback`, {
    ...e.body,
    reasons: e.body.reasons ?? [],
    source: 'ui',
    client_event_id: e.client_event_id,
    generation: e.generation,
    client_instance_id: e.client_instance_id,
    client_seq: e.client_seq,
    client_created_at: e.client_created_at,
  }, { validateStatus: () => true }).then(r => ({ status: r.status, data: r.data }))

export interface ExposureItem {
  job_id: string
  surface: string
  position?: number | null
  visible_ms?: number | null
  sort_key?: string | null
  randomized?: boolean
  client_created_at?: string | null
}

/** Best-effort: a failure or a stale generation simply loses the batch. */
export const postExposureBatch = (generation: string | null, session_id: string, items: ExposureItem[]) =>
  api.post<{ stored: number }>('/jobs/feedback/exposure', { generation, session_id, items })
    .then(r => r.data.stored).catch(() => 0)

// I18 outcome tracking: fire-and-forget, best-effort. Only "viewed" and
// "apply_link_opened" may be reported this way — never let a failure here
// affect navigation or the surrounding UI.
export const recordJobEvent = (id: string, eventType: 'viewed' | 'apply_link_opened') =>
  api.post(`/jobs/${id}/events`, { event_type: eventType }).catch(() => undefined)
