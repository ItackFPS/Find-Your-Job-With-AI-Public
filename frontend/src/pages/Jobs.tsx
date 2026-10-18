import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { listJobs, getAllDepartments, getJobCountries, getProfile, type Job, type CountryFacet, type LocationPreference } from '../api/client'
import { Link } from 'react-router-dom'
import { Chip, EmptyState, LoadingState, Ring } from '../components'
import { daysAgo, getSourceBadge, locationBadge } from '../utils'

// ---------------------------------------------------------------------------
// Tier presentation — labels/colors only; the tier VALUE itself always comes
// from the backend (job.score.tier, via the canonical score_to_tier()).
// ---------------------------------------------------------------------------

const TIER_ORDER = ['exceptional', 'excellent', 'strong', 'relevant', 'low_priority'] as const
type Tier = typeof TIER_ORDER[number]

const TIER_META: Record<Tier, { label: string; color: string }> = {
  exceptional: { label: 'Exceptional', color: 'var(--color-accent)' },
  excellent: { label: 'Excellent', color: 'var(--color-info)' },
  strong: { label: 'Strong', color: '#5b9ee8' },
  relevant: { label: 'Relevant', color: 'var(--color-warning)' },
  low_priority: { label: 'Low priority', color: 'var(--color-muted)' },
}

function TierBadge({ tier }: { tier: string | null }) {
  const meta = tier && tier in TIER_META ? TIER_META[tier as Tier] : null
  if (!meta) return null
  return (
    <span
      style={{
        fontSize: '0.68rem', fontWeight: 'var(--font-weight-bold)',
        color: meta.color, border: `1px solid ${meta.color}`,
        borderRadius: 'var(--radius-sm)', padding: '0.1rem 0.4rem',
        whiteSpace: 'nowrap',
      }}
    >
      {meta.label}
    </span>
  )
}

const LOCATION_KIND_COLOR: Record<string, string> = {
  preferred: 'var(--color-accent)',
  paca: 'var(--color-info)',
  'france-remote': 'var(--color-success)',
  'international-remote': '#5b9ee8',
  other: 'var(--color-muted)',
}

function LocationBadgeChip({ job, locationPreferences, departmentNames }: {
  job: Job
  locationPreferences: LocationPreference[]
  departmentNames: Record<string, string>
}) {
  const badge = locationBadge(job, locationPreferences, departmentNames)
  const color = LOCATION_KIND_COLOR[badge.kind]
  return (
    <span style={{ fontSize: '0.78rem', color, fontWeight: badge.kind === 'preferred' ? 'var(--font-weight-semibold)' : undefined }}>
      {badge.label}
    </span>
  )
}

// ---------------------------------------------------------------------------
// RelevantSkills — the target skills (from the profile) this job actually
// mentions, mirroring the explanation engine's "Mentions relevant skills"
// positive reason exactly (src/pipeline/explain.py::_matched_skills: a
// diacritic-insensitive substring match against the job's title + first
// 3000 chars of its description). The job's own tagged `skills` field is
// often empty even when the description clearly mentions a target skill —
// matching the real text, like the backend does, is what actually finds it.
// ---------------------------------------------------------------------------

// Mirrors src/pipeline/explain.py::_norm (NFD + strip combining marks,
// category "Mn") for the same accent-insensitive matching.
function normalizeForMatch(text: string): string {
  return text.normalize('NFD').replace(/\p{Mn}/gu, '').toLowerCase()
}

function matchedSkills(job: Job, targetSkills: string[]): string[] {
  const text = normalizeForMatch(`${job.title ?? ''} ${(job.description ?? '').slice(0, 3000)}`)
  const matches = targetSkills.filter(s => text.includes(normalizeForMatch(s)))
  // The profile can hold case-variant duplicates (e.g. "Docker" and "docker"
  // — TagList's own dedup check is case-sensitive), which would otherwise
  // render as two identical-looking chips once lowercased for display.
  const seen = new Set<string>()
  return matches.filter(s => {
    const key = s.toLowerCase()
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function RelevantSkills({ job, targetSkills }: { job: Job; targetSkills: string[] }) {
  const matched = matchedSkills(job, targetSkills)
  if (matched.length === 0) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginTop: '0.5rem' }}>
      {matched.slice(0, 3).map(s => <Chip key={s}>{s.toLowerCase()}</Chip>)}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ScoreBar
// ---------------------------------------------------------------------------

function ScoreBar({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const color = pct >= 70 ? 'var(--color-success)' : pct >= 40 ? 'var(--color-warning)' : 'var(--color-muted)'
  return (
    <Ring value={pct} color={color} size={46} strokeWidth={5}>
      <span style={{ fontSize: '0.8rem', fontWeight: 'var(--font-weight-bold)' }}>{pct}</span>
    </Ring>
  )
}

// ---------------------------------------------------------------------------
// SourceAvatar — small colored initial badge for a job's source
// ---------------------------------------------------------------------------

function SourceAvatar({ source }: { source: string }) {
  const badge = getSourceBadge(source)
  return (
    <div
      title={source}
      style={{
        width: '32px',
        height: '32px',
        borderRadius: '50%',
        background: badge.color,
        color: '#fff',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: '0.68rem',
        fontWeight: 'var(--font-weight-bold)',
        flexShrink: 0,
      }}
    >
      {badge.label}
    </div>
  )
}

// ---------------------------------------------------------------------------
// JobCard
// ---------------------------------------------------------------------------

const cardLinkStyle: React.CSSProperties = {
  background: 'var(--color-surface)',
  border: '1px solid var(--color-border)',
  borderRadius: 'var(--radius-lg)',
  padding: '1rem 1.25rem',
  marginBottom: '0.6rem',
  textDecoration: 'none',
  display: 'block',
  color: 'inherit',
  transition: 'border-color 0.15s',
}

function JobCard({ job, targetSkills, locationPreferences, departmentNames }: {
  job: Job
  targetSkills: string[]
  locationPreferences: LocationPreference[]
  departmentNames: Record<string, string>
}) {
  const finalScore = job.score?.final_score ?? null
  const tier = job.score?.tier ?? null
  const age = daysAgo(job.published_at)

  return (
    <Link
      to={`/jobs/${job.id}`}
      style={cardLinkStyle}
      onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--color-accent)')}
      onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '0.5rem', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', gap: '0.65rem', alignItems: 'center' }}>
          <SourceAvatar source={job.source} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: '0.95rem', marginBottom: '0.2rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {job.title || '(no title)'}
            </div>
            <div style={{ fontSize: '0.82rem', color: 'var(--color-muted)', display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
              <span>{[job.company].filter(Boolean).join(' · ')}</span>
              <LocationBadgeChip job={job} locationPreferences={locationPreferences} departmentNames={departmentNames} />
            </div>
          </div>
        </div>
        {finalScore !== null && <ScoreBar value={finalScore} />}
      </div>

      <RelevantSkills job={job} targetSkills={targetSkills} />

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '0.85rem' }}>
        <TierBadge tier={tier} />
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '0.5rem' }}>
        <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)' }}>
          {job.source}{age !== null ? ` · ${age}d ago` : ''}
        </div>
      </div>
    </Link>
  )
}

// ---------------------------------------------------------------------------
// Jobs page
// ---------------------------------------------------------------------------

const PAGE = 50

// Default dashboard visibility floor: hide low_priority jobs by default
// (they remain fully stored/queryable — this is a presentation default
// only, switched off via the "Min tier" select below, never a data change).
const DEFAULT_MIN_TIER = 'relevant'

export default function Jobs() {
  const [country, setCountry] = useState('')
  const [department, setDepartment] = useState('')   // e.g. "83" for Var
  const [remote, setRemote] = useState<boolean | undefined>(undefined)
  const [minTier, setMinTier] = useState(DEFAULT_MIN_TIER)
  const [q, setQ] = useState('')         // debounced value sent to API
  const [inputQ, setInputQ] = useState('') // immediate input value
  const [offset, setOffset] = useState(0)

  // Search-as-you-type: fire after 250 ms idle, min 2 chars (or empty to clear).
  useEffect(() => {
    const shouldFire = inputQ.length === 0 || inputQ.length >= 2
    if (!shouldFire) return
    const h = setTimeout(() => {
      setQ(inputQ)
      setOffset(0)
    }, 250)
    return () => clearTimeout(h)
  }, [inputQ])

  // Department filter is only meaningful when filtering by France.
  const isFrance = country.trim().toLowerCase() === 'france'

  // Load all departments once (static data, cache forever).
  const { data: allDepartments } = useQuery({
    queryKey: ['departments-all'],
    queryFn: getAllDepartments,
    staleTime: Infinity,
  })

  // Load country facets (job counts per country) from the latest run.
  const { data: countriesData } = useQuery({
    queryKey: ['job-countries'],
    queryFn: getJobCountries,
    staleTime: 60_000,
  })
  const totalJobCount = countriesData?.total ?? null
  const countryFacets: CountryFacet[] = countriesData?.facets ?? []

  // Loaded once for the "Mentions relevant skills" line on each card.
  const { data: profile } = useQuery({ queryKey: ['profile'], queryFn: getProfile, staleTime: 60_000 })
  const targetSkills = profile?.target_skills ?? []
  const locationPreferences = profile?.geography.location_preferences ?? []
  const departmentNames: Record<string, string> = Object.fromEntries(
    (allDepartments ?? []).map(d => [d.code, d.name])
  )

  const { data: jobs, isLoading, error } = useQuery({
    queryKey: ['jobs', country, isFrance ? department : '', remote, minTier, q, offset],
    queryFn: () => listJobs({
      country: country || undefined,
      department: (isFrance && department) ? department : undefined,
      remote,
      min_tier: minTier !== 'low_priority' ? minTier : undefined,
      q: q || undefined,
      limit: PAGE,
      offset,
    }),
  })

  const filterStyle: React.CSSProperties = {
    background: 'var(--color-surface)',
    border: '1px solid var(--color-border)',
    borderRadius: 'var(--radius-sm)',
    padding: '0.4rem 0.75rem',
    color: 'var(--color-text)',
    fontSize: '0.85rem',
    outline: 'none',
  }

  const hasActiveFilter = !!(
    country || remote !== undefined || q || (isFrance && department) || minTier !== DEFAULT_MIN_TIER
  )

  return (
    <div>
      <h1 style={{ margin: '0 0 1.25rem', fontSize: 'var(--font-size-xl)', fontWeight: 'var(--font-weight-bold)' }}>Jobs</h1>

      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', marginBottom: '1.25rem', alignItems: 'center' }}>
        {/* Full-text search — fires as you type (250 ms debounce, min 2 chars) */}
        <input
          value={inputQ}
          onChange={e => setInputQ(e.target.value)}
          placeholder="Search jobs…"
          style={{ ...filterStyle, flex: 1, minWidth: '200px' }}
        />

        {/* Country — selecting resets department; France enables department filter */}
        <select
          value={country}
          onChange={e => {
            setCountry(e.target.value)
            setDepartment('')
            setOffset(0)
          }}
          style={{ ...filterStyle, width: '200px' }}
        >
          <option value="">
            {totalJobCount !== null ? `All countries (${totalJobCount})` : 'All countries'}
          </option>
          {countryFacets.map(f => (
            <option key={f.name} value={f.name}>
              {f.name === '_worldwide' ? `Worldwide / Remote (${f.count})` : `${f.name} (${f.count})`}
            </option>
          ))}
        </select>

        {/* Department — enabled only when Country = France */}
        <select
          value={department}
          onChange={e => { setDepartment(e.target.value); setOffset(0) }}
          disabled={!isFrance}
          title={isFrance ? 'Filter by French department' : 'Set Country to France to filter by department'}
          style={{
            ...filterStyle,
            width: '180px',
            opacity: isFrance ? 1 : 0.45,
            cursor: isFrance ? 'pointer' : 'not-allowed',
          }}
        >
          <option value="">All departments</option>
          {(allDepartments ?? []).map(dept => (
            <option key={dept.code} value={dept.code}>
              {dept.name} ({dept.code})
            </option>
          ))}
        </select>

        {/* Work mode */}
        <select
          value={remote === undefined ? '' : remote ? 'true' : 'false'}
          onChange={e => {
            setOffset(0)
            if (e.target.value === '') setRemote(undefined)
            else setRemote(e.target.value === 'true')
          }}
          style={filterStyle}
        >
          <option value="">All modes</option>
          <option value="true">Remote only</option>
          <option value="false">On-site only</option>
        </select>

        {/* Min tier — dashboard visibility floor. Default hides low_priority;
            jobs stay fully stored/queryable, "All tiers" shows everything. */}
        <select
          value={minTier}
          onChange={e => { setMinTier(e.target.value); setOffset(0) }}
          title="Minimum tier to show (jobs below this stay stored — pick 'All tiers' to see everything)"
          style={filterStyle}
        >
          <option value="exceptional">Exceptional only</option>
          <option value="excellent">Excellent and above</option>
          <option value="strong">Strong and above</option>
          <option value="relevant">Relevant and above (default)</option>
          <option value="low_priority">All tiers</option>
        </select>

        {hasActiveFilter && (
          <button
            onClick={() => {
              setInputQ('')
              setQ('')
              setCountry('')
              setDepartment('')
              setRemote(undefined)
              setMinTier(DEFAULT_MIN_TIER)
              setOffset(0)
            }}
            style={{ ...filterStyle, cursor: 'pointer', color: 'var(--color-muted)' }}
          >
            Clear
          </button>
        )}
      </div>

      {isLoading && <LoadingState compact />}
      {error && <div style={{ color: 'var(--color-error)' }}>Failed to load jobs.</div>}

      {jobs && jobs.length === 0 && (
        <EmptyState message="No jobs found. Try adjusting your filters or running the pipeline." />
      )}

      {jobs && jobs.map(job => (
        <JobCard
          key={job.id}
          job={job}
          targetSkills={targetSkills}
          locationPreferences={locationPreferences}
          departmentNames={departmentNames}
        />
      ))}

      {jobs && (jobs.length === PAGE || offset > 0) && (
        <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'center', marginTop: '1rem' }}>
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE))}
            disabled={offset === 0}
            style={{
              background: 'var(--color-surface)',
              border: '1px solid var(--color-border)',
              borderRadius: 'var(--radius-sm)',
              padding: '0.4rem 1rem',
              color: offset === 0 ? 'var(--color-border)' : 'var(--color-text)',
              cursor: offset === 0 ? 'not-allowed' : 'pointer',
              fontSize: '0.85rem',
            }}
          >
            ← Prev
          </button>
          <span style={{ lineHeight: '2rem', fontSize: '0.82rem', color: 'var(--color-muted)' }}>
            {offset + 1}–{offset + (jobs?.length || 0)}
          </span>
          <button
            onClick={() => setOffset(offset + PAGE)}
            disabled={!jobs || jobs.length < PAGE}
            style={{
              background: 'var(--color-surface)',
              border: '1px solid var(--color-border)',
              borderRadius: 'var(--radius-sm)',
              padding: '0.4rem 1rem',
              color: (!jobs || jobs.length < PAGE) ? 'var(--color-border)' : 'var(--color-text)',
              cursor: (!jobs || jobs.length < PAGE) ? 'not-allowed' : 'pointer',
              fontSize: '0.85rem',
            }}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
}
