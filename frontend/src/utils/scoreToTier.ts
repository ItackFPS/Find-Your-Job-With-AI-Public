export type ScoreTier = 'exceptional' | 'excellent' | 'strong' | 'relevant' | 'low_priority'

/**
 * Display-only mirror of the backend's tier thresholds
 * (`score_to_tier` in src/pipeline/scoring.py). This is never a second
 * implementation of scoring — it exists purely so the UI can label a
 * score_100 value the same way the backend does.
 *
 * Phase 4 (2026-09-10): fixed-reference calibration, derived once from a
 * 4-run/30,271-job real historical sample (see the full calibration
 * methodology and reference percentiles documented at
 * src/pipeline/scoring.py's `_TIER_THRESHOLDS`). NOT live per-run
 * percentiles — these are hardcoded constants that do not move with the
 * current job pool. If they are ever recalibrated, update
 * src/pipeline/scoring.py first and mirror the same values here.
 */
export function scoreToTier(score100: number): ScoreTier {
  if (score100 >= 80) return 'exceptional'
  if (score100 >= 72) return 'excellent'
  if (score100 >= 65) return 'strong'
  if (score100 >= 60) return 'relevant'
  return 'low_priority'
}
