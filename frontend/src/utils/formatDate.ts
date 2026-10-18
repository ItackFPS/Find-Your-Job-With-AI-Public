export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString()
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString()
}

/** Whole days between now and `iso`, or null when there's no date. */
export function daysAgo(iso: string | null | undefined): number | null {
  if (!iso) return null
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
}
