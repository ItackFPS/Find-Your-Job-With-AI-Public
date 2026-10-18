import type { LocationPreference } from '../api/client'

// Location Priority feature (2026-09-10): display-only location grouping.
// Mirrors the fixed, rarely-changing constant PACA_NEIGHBOR_DEPARTMENTS in
// src/pipeline/departments.py (used by location_fit scoring) — this label
// never feeds back into scoring or Discord routing, it only groups an
// already-resolved `department` for display. "preferred" here covers BOTH
// LocationPreference statuses "preferred" and "acceptable" (a display
// simplification only); "excluded" entries are never treated as preferred.
const PACA_NEIGHBOR_DEPARTMENTS = new Set(['13', '04', '05', '06', '84'])

export interface LocationBadge {
  label: string
  kind: 'preferred' | 'paca' | 'france-remote' | 'international-remote' | 'other'
}

const DEFAULT_DEPARTMENT_NAMES: Record<string, string> = {}

/**
 * Classify a job's location for display: any of the candidate's configured
 * department preferences (profile.geography.location_preferences, status
 * preferred/acceptable) is called out distinctly, PACA-neighbor departments
 * next, then remote (France vs. international), falling back to the raw
 * location string. Never a hard filter — every job still renders, just
 * labeled. No location is implied when the profile has none configured.
 */
export function locationBadge(
  job: {
    department?: string | null
    countries?: string[]
    work_mode?: string | null
    remote?: boolean
    location?: string | null
  },
  locationPreferences: LocationPreference[] = [],
  departmentNames: Record<string, string> = DEFAULT_DEPARTMENT_NAMES,
): LocationBadge {
  const dept = job.department ?? null
  const isRemote = job.work_mode === 'remote' || (job.work_mode == null && !!job.remote)
  const isFrance = (job.countries ?? []).some(c => c.toLowerCase() === 'france')

  const preferredDepartments = locationPreferences
    .filter(p => p.type === 'department' && p.status !== 'excluded')
    .map(p => p.value)

  if (dept && preferredDepartments.includes(dept)) {
    const name = departmentNames[dept]
    return { label: name ? `${name} (${dept})` : dept, kind: 'preferred' }
  }
  if (dept && PACA_NEIGHBOR_DEPARTMENTS.has(dept)) return { label: `PACA (${dept})`, kind: 'paca' }
  if (isRemote) {
    return isFrance
      ? { label: 'France · Remote', kind: 'france-remote' }
      : { label: 'International · Remote', kind: 'international-remote' }
  }
  return { label: job.location || 'Unknown location', kind: 'other' }
}
