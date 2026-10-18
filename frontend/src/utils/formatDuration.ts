/** Formats a duration in seconds as e.g. "12.3s". Non-positive/missing → "–". */
export function formatDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || seconds <= 0) return '–'
  return `${seconds.toFixed(1)}s`
}

/** Formats the elapsed time between two ISO timestamps, or "–" while `endIso` is still null. */
export function formatElapsedBetween(startIso: string, endIso: string | null): string {
  if (!endIso) return '–'
  const seconds = (new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000
  return formatDuration(seconds)
}
