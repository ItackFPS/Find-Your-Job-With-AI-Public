/** Formats a duration in seconds as e.g. "12.3s". Non-positive/missing → "–". */
export function formatDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || seconds <= 0) return '–'
  return `${seconds.toFixed(1)}s`
}

/** A finished run's real duration (Phase B B7-B): its wall clock, completed_at - started_at, which
 *  covers the journal, expiry and saving too. Falls back to the internal duration when a timestamp
 *  is missing. */
export function runWallSeconds(run: { started_at?: string | null; completed_at?: string | null;
  stats?: { duration_seconds?: number } }): number | null {
  if (run.started_at && run.completed_at) {
    const seconds = (new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()) / 1000
    if (Number.isFinite(seconds) && seconds > 0) return seconds
  }
  return run.stats?.duration_seconds ?? null
}

/** Formats the elapsed time between two ISO timestamps, or "–" while `endIso` is still null. */
export function formatElapsedBetween(startIso: string, endIso: string | null): string {
  if (!endIso) return '–'
  const seconds = (new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000
  return formatDuration(seconds)
}
