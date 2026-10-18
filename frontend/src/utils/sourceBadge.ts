export interface SourceBadge {
  label: string
  color: string
}

// Explicit badges for the sources actually seen in the DB (see config/sources.yaml
// plus the LinkedIn/HelloWork staging scanners). Anything else falls back to a
// derived label/color below rather than going unstyled.
const EXACT: Record<string, SourceBadge> = {
  'france-travail': { label: 'FT', color: '#5b6280' },
  linkedin: { label: 'L', color: '#0a66c2' },
  indeed: { label: 'I', color: '#1e2a3a' },
  eures: { label: 'E', color: '#003399' },
  apec: { label: 'A', color: '#7c6ef5' },
  hellowork: { label: 'H', color: '#f4726b' },
  jobicy: { label: 'J', color: '#f59e0b' },
  remoteok: { label: 'R', color: '#2dd4bf' },
  weworkremotely: { label: 'W', color: '#0891b2' },
  'weworkremotely-rss': { label: 'W', color: '#0891b2' },
  himalayas: { label: 'Hi', color: '#6366f1' },
  'arbeitnow-public': { label: 'AN', color: '#64748b' },
  workingnomads: { label: 'WN', color: '#84cc16' },
  remotive: { label: 'Rm', color: '#22c55e' },
}

// Per-company ATS scrapes are named "<platform>-<company>" (e.g.
// "greenhouse-doctolib") — badge by platform, company shows in the tooltip.
const ATS_PREFIXES: Record<string, SourceBadge> = {
  greenhouse: { label: 'G', color: '#22c55e' },
  ashby: { label: 'As', color: '#f97316' },
  lever: { label: 'Lv', color: '#6366f1' },
  smartrecruiters: { label: 'SR', color: '#ef4444' },
}

const FALLBACK_COLORS = ['#8892b0', '#60a5fa', '#4ade80', '#fbbf24', '#f87171', '#7c6ef5']

function hashColor(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return FALLBACK_COLORS[h % FALLBACK_COLORS.length]
}

/** Small badge (label + color) for a job's `source` string — deterministic, never invented per-job. */
export function getSourceBadge(source: string): SourceBadge {
  const key = source.toLowerCase()
  if (EXACT[key]) return EXACT[key]
  const prefix = key.split('-')[0]
  if (ATS_PREFIXES[prefix]) return ATS_PREFIXES[prefix]
  return { label: source.slice(0, 2).toUpperCase(), color: hashColor(key) }
}
