import type { HelloWorkScanProgress, LinkedInScanProgress, ScanState } from '../api/client'
import i18n from '../i18n'

// Which control a scanner card shows, derived from the backend's persisted
// scan state (issue #81):
//   running     → Pause
//   pausing     → "Pausing…" (disabled until the checkpoint boundary)
//   paused      → Resume (survives restarts until clicked)
//   interrupted → Resume
//   idle        → Scan now
// Older backends without `state` fall back to is_scanning.
// Interrupted scans are also resumed by the scheduler on its own when the
// source has an enabled daily schedule.

export type ScannerAction = 'scan' | 'pause' | 'resume' | 'none'

export interface ScannerControl {
  state: ScanState
  action: ScannerAction
  label: string
  disabled: boolean
}

export function scannerState(status: { is_scanning: boolean; state?: ScanState }): ScanState {
  return status.state ?? (status.is_scanning ? 'running' : 'idle')
}

export function scannerControl(
  status: { is_scanning: boolean; state?: ScanState },
  pending: boolean = false,
  // A Scan now/Resume was just accepted but the backend is not scanning yet:
  // no control until it is, since a pause would have nothing to stop.
  starting: boolean = false,
): ScannerControl {
  const t = i18n.t.bind(i18n)
  if (starting) return { state: 'running', action: 'none', label: t('scanner.starting'), disabled: true }
  const state = scannerState(status)
  switch (state) {
    case 'running':
      return { state, action: 'pause', label: t('scanner.pause'), disabled: pending }
    case 'pausing':
      return { state, action: 'none', label: t('scanner.pausing'), disabled: true }
    case 'paused':
    case 'interrupted':
      return { state, action: 'resume', label: pending ? t('scanner.resuming') : t('scanner.resume'), disabled: pending }
    default:
      return { state, action: 'scan', label: pending ? t('scanner.starting') : t('scanner.scanNow'), disabled: pending }
  }
}

// After Scan now / Resume is accepted the card holds "Starting…" until the
// backend confirms the scan: its status reports is_scanning, or a different
// last scan (a run that started and already ended between two polls). The
// status cached from before the click satisfies neither, so it can no longer
// end the hold early — a Resume is dispatched asynchronously by the scheduler
// (issue #81 review, F5). The time bound is only a safety net for a start that
// never materialises; it is not the synchronisation mechanism.
export interface StartHold {
  fromScanId: string | null
  since: number
}

export const START_HOLD_SAFETY_MS = 60_000

export function startHoldEnded(
  hold: StartHold,
  status: { is_scanning: boolean; last_scan: { id: string } | null },
  now: number,
): boolean {
  if (status.is_scanning) return true
  if ((status.last_scan?.id ?? null) !== hold.fromScanId) return true
  return now - hold.since > START_HOLD_SAFETY_MS
}

// Real checkpoint progress only — no invented percentages. LinkedIn's page
// totals are the backend's estimate (issue #86), hence the "~".
export function scannerProgressLabel(
  progress: LinkedInScanProgress | HelloWorkScanProgress | null | undefined,
): string | null {
  if (!progress) return null
  if ('queries_total' in progress) {
    const query = Math.min(progress.queries_completed + 1, progress.queries_total)
    const page = progress.current_query ? i18n.t('scanner.progress.page', { page: progress.current_page + 1 }) : ''
    const pages = progress.pages_total_est
      ? i18n.t('scanner.progress.pages', { done: progress.pages_done_est ?? 0, total: progress.pages_total_est })
      : ''
    return i18n.t('scanner.progress.query', { query, total: progress.queries_total }) + page + pages
  }
  return i18n.t('scanner.progress.metierPages', { count: progress.slugs_completed })
}

// The backend's work-based estimate (LinkedIn, issue #86), or null for a
// source without one (HelloWork), which keeps its time-based estimate.
export function scannerWorkEstimate(
  progress: LinkedInScanProgress | HelloWorkScanProgress | null | undefined,
): { percent: number; etaSeconds: number | null } | null {
  if (!progress || !('percent' in progress) || typeof progress.percent !== 'number') return null
  return { percent: progress.percent, etaSeconds: progress.eta_seconds ?? null }
}
