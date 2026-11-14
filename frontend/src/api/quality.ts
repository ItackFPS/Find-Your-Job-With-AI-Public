import { api } from './http'
import type { SelfCheckStatus } from './lab'

// ---- Quality page: background self-check / evaluation jobs (src/api/routes/quality.py) ----
// Summaries are codes and numbers only; the page words them in the user's language.
export type QualityJobKind = 'self_check' | 'evaluation' | 'personal'
export type QualityJobStatus = 'running' | 'completed' | 'failed' | 'cancelled'
export type QualityPhase = 'starting' | 'running' | 'summarising'
export type QualityFlag =
  | 'synthetic_benchmark' | 'few_suitable_labels' | 'labels_not_independent' | 'review_insufficient_evidence'
  | 'baseline_missing' | 'baseline_mismatch'
export interface QualityDatasetSummary {
  id: string | null; version: string | null; kind: string | null; n_jobs: number | null; suitable: number | null
  status: SelfCheckStatus | null; unsafe_rejections: number; rejections_without_reason: number | null
  false_positives: number | null; precision: number | null; recall: number | null; ranking: string
  review: string | null; baseline: string | null; flips: { fixed: number; broken: number } | null
  flags: QualityFlag[]; warnings?: number
}
export type QualityFeedbackSummary =
  | { status: 'not_requested' | 'unavailable' }
  | { status: 'measured'; answered: number | null; decided: number | null; coverage: number | null; contradictions: number }
export interface QualitySummary {
  kind: QualityJobKind; status: SelfCheckStatus | 'measured' | null; created_at: string | null
  datasets: QualityDatasetSummary[]; feedback: QualityFeedbackSummary; report_file: string | null
}
export interface QualityJob {
  id: string; kind: QualityJobKind; status: QualityJobStatus; phase: QualityPhase | null
  options: Record<string, unknown>; created_at: string; finished_at: string | null
  summary: QualitySummary | PersonalSummary | null; error_code: string | null
}
type Interval = [number, number] | null
export interface PersonalSummary {
  kind: 'personal'; created_at: string | null; report_file: string | null; flags: string[]; warnings: number
  dataset: { id: string | null; version: string | null; split: string | null; labels: number | null; accept_labels: number }
  ranking: {
    auc: number | null; auc_ci95: Interval; n_accept: number | null; n_reject: number | null
    precision_at_strong: { value: number | null; ci95: Interval; n: number | null }
    recall_at_20: { value: number | null; ci95: Interval; in_top: number | null; n_accept: number | null }
  }
  representative_auc: number | null
  missed: { labelled: number | null; accept: number | null; rate_ci95: Interval; estimate: [number, number, number] | null; pool: number | null } | null
  unsafe_rejections: number; unsafe_freshness_only: number | null
  false_positives: { total: number; by_category: Record<string, number>; by_source: Record<string, number> }
}
export interface QualityHealth { self_check: QualitySummary | null; evaluation: QualitySummary | null }
export const getQualityHealth = () => api.get<QualityHealth>('/quality/health').then(r => r.data)
export const listQualityJobs = (limit = 20) =>
  api.get<{ active_id: string | null; jobs: QualityJob[] }>(`/quality/jobs?limit=${limit}`).then(r => r.data)
export const getQualityJob = (id: string) => api.get<QualityJob>(`/quality/jobs/${id}`).then(r => r.data)
export const startQualityJob = (kind: QualityJobKind, options?: Record<string, unknown>) =>
  api.post<QualityJob>('/quality/jobs', options ? { kind, options } : { kind }).then(r => r.data)
export const cancelQualityJob = (id: string) =>
  api.post<{ id: string; cancelling: boolean }>(`/quality/jobs/${id}/cancel`).then(r => r.data)
export interface FtCoverage {
  status: 'measured' | 'too_early' | 'no_api_offers' | 'no_web_offers'; window_days: number; days_with_both: number
  api_offers: number; web_offers: number; found_by_web: number; share: number | null; ci95: [number, number] | null
}
export const getFtCoverage = () => api.get<FtCoverage>('/quality/ft-coverage').then(r => r.data)

/** Requirement extractor vs France Travail structured fields (v5 L2): counts and rates only. */
export interface AgreementRate { k: number; n: number; value: number | null; ci95: [number, number] | null }
export interface ExtractorAgreement {
  status: 'measured' | 'no_france_travail_offers'; offers: number; limit: number; years_tolerance: number
  rome_coverage: AgreementRate
  experience: { precision: AgreementRate; recall: AgreementRate; disagreement: AgreementRate; beginner_conflicts: number }
  education: { precision: AgreementRate; recall: AgreementRate; disagreement: AgreementRate }
}
export const getExtractorAgreement = () =>
  api.get<ExtractorAgreement>('/quality/extractor-agreement').then(r => r.data)

/** Self-monitoring alerts (ADR 0025): a code, a scope and its evidence; the page words them. */
export interface OpsAlert {
  alert_id: string; raised_at: string; updated_at: string; scope: string
  code: 'source_volume_drop' | 'field_completeness_drop' | string; severity: string
  status: 'open' | 'resolved'; resolved_at: string | null; resolved_by: string | null
  evidence: Record<string, unknown>
}
export const getOpsAlerts = () => api.get<{ alerts: OpsAlert[] }>('/quality/alerts').then(r => r.data.alerts)
export const resolveOpsAlert = (id: string) =>
  api.post<{ alert_id: string; status: string }>(`/quality/alerts/${encodeURIComponent(id)}/resolve`).then(r => r.data)

// ---- Quality › Calibration: blind labelling of an exported real dataset (P2.3) ----
export type CalibrationAnswer = 'relevant' | 'not_relevant' | 'not_sure'
export interface CalibrationDataset {
  dataset_id: string; version?: string; total?: number; labelled?: number; pending?: number; target?: number
  by_answer?: Record<CalibrationAnswer, number>; provenance?: Record<string, number>; error?: string
}
export interface CalibrationOverview {
  datasets: CalibrationDataset[]; exports_found: { source: string; dataset_id: string; total: number }[]
  reasons: string[]; max_session: number
}
export interface CalibrationItem {
  job_id: string; title: string; company: string; location: string; remote: boolean | null; source: string
  description: string; description_truncated: boolean
}
export const getCalibration = () => api.get<CalibrationOverview>('/quality/calibration').then(r => r.data)
export const importCalibration = (source: string) =>
  api.post<CalibrationDataset>('/quality/calibration/import', { source }).then(r => r.data)
export const nextCalibrationItems = (id: string, n: number) =>
  api.get<{ items: CalibrationItem[] }>(`/quality/calibration/${encodeURIComponent(id)}/next?n=${n}`).then(r => r.data.items)
export const answerCalibration = (id: string, job_id: string, answer: CalibrationAnswer, reason: string | null) =>
  api.post<CalibrationDataset>(`/quality/calibration/${encodeURIComponent(id)}/answers`, { job_id, answer, reason }).then(r => r.data)
export const saveCalibration = (id: string) =>
  api.post<CalibrationDataset & { changed: number }>(`/quality/calibration/${encodeURIComponent(id)}/save`).then(r => r.data)
export const extendCalibration = (id: string) =>
  api.post<CalibrationDataset>(`/quality/calibration/${encodeURIComponent(id)}/extend`).then(r => r.data)
