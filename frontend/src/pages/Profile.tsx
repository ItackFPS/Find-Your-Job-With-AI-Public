import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getProfile, updateProfile, searchEscoSkills, searchEscoOccupations,
  getAllDepartments,
  type Profile, type EscoEntry, type EscoSuggestion, type FrenchDepartment, type LocationPreference,
} from '../api/client'
import { Card, LoadingState, Switch } from '../components'

// Prevents "Docker" and "docker" (or "French"/"french") coexisting as two
// separate entries — an exact-string check alone lets that through.
function includesCaseInsensitive(values: string[], candidate: string): boolean {
  const target = candidate.toLowerCase()
  return values.some(v => v.toLowerCase() === target)
}

// ---------------------------------------------------------------------------
// Shared styles
// ---------------------------------------------------------------------------

const inputStyle: React.CSSProperties = {
  background: 'var(--color-bg)',
  border: '1px solid var(--color-border)',
  borderRadius: '7px',
  padding: '0.5rem 0.8rem',
  color: 'var(--color-text)',
  fontSize: '0.9rem',
  outline: 'none',
  width: '100%',
  boxSizing: 'border-box',
}

const label: React.CSSProperties = {
  fontSize: '0.8rem',
  fontWeight: 600,
  color: 'var(--color-muted)',
  marginBottom: '0.6rem',
  display: 'flex',
  alignItems: 'baseline',
  gap: '0.45rem',
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
}

function CountBadge({ count }: { count: number }) {
  return (
    <span style={{ fontSize: '0.72rem', fontWeight: 400, color: 'var(--color-muted)', textTransform: 'none', letterSpacing: 'normal' }}>
      {count}
    </span>
  )
}

const fieldGroup: React.CSSProperties = {
  marginBottom: '1.4rem',
}

const sectionDivider: React.CSSProperties = {
  border: 'none',
  borderTop: '1px solid var(--color-border)',
  margin: '1.5rem 0',
}

type TagTone = 'language' | 'default'

const TAG_TONE_STYLES: Record<TagTone, { border: string }> = {
  language: { border: 'var(--color-info)66' },
  default: { border: 'var(--color-border)' },
}

// ---------------------------------------------------------------------------
// Merged-field shared styling — chips and the input live inside one bordered
// box (instead of a chip row sitting above a separate input row), with an
// accent focus ring. Shared by TagList, AutocompleteTagList and DomainPicker.
// ---------------------------------------------------------------------------

function mergeFieldStyle(focused: boolean): React.CSSProperties {
  return {
    background: 'var(--color-bg)',
    border: `1px solid ${focused ? 'var(--color-accent)' : 'var(--color-border)'}`,
    borderRadius: '9px',
    padding: '0.6rem',
    display: 'flex',
    flexWrap: 'wrap',
    gap: '0.5rem',
    alignItems: 'center',
    minHeight: '48px',
    boxShadow: focused ? '0 0 0 3px var(--color-accent)22' : undefined,
    transition: 'border-color 0.15s, box-shadow 0.15s',
    position: 'relative',
  }
}

const mergeInputStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  outline: 'none',
  color: 'var(--color-text)',
  fontSize: '0.9rem',
  flex: 1,
  minWidth: '160px',
  padding: '0.3rem 0.2rem',
}

const chipRemoveStyle: React.CSSProperties = {
  background: 'var(--color-surface-elevated)',
  border: 'none',
  borderRadius: '50%',
  color: 'var(--color-muted)',
  cursor: 'pointer',
  width: '18px',
  height: '18px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: '0.85rem',
  lineHeight: 1,
  padding: 0,
  flex: 'none',
}

const dropdownStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 6px)',
  left: 0,
  right: 0,
  background: 'var(--color-surface)',
  border: '1px solid var(--color-border)',
  borderRadius: '9px',
  boxShadow: '0 8px 20px rgba(0,0,0,0.35)',
  zIndex: 100,
  overflow: 'hidden',
}

// ---------------------------------------------------------------------------
// TagList — plain comma / Enter tag entry (for languages, preferred countries)
// ---------------------------------------------------------------------------

function TagList({ values, onChange, tone = 'default', placeholder = 'Add and press Enter…' }: {
  values: string[]; onChange: (v: string[]) => void; tone?: TagTone; placeholder?: string
}) {
  const [draft, setDraft] = useState('')
  const [focused, setFocused] = useState(false)
  const toneStyle = TAG_TONE_STYLES[tone]

  const add = () => {
    const trimmed = draft.trim()
    if (trimmed && !includesCaseInsensitive(values, trimmed)) {
      onChange([...values, trimmed])
    }
    setDraft('')
  }

  return (
    <div style={mergeFieldStyle(focused)}>
      {values.map(v => (
        <span key={v} style={{
          background: 'var(--color-surface-elevated)',
          border: `1px solid ${toneStyle.border}`,
          borderRadius: '7px',
          padding: '0.35rem 0.45rem 0.35rem 0.8rem',
          fontSize: '0.86rem',
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
        }}>
          {v}
          <button onClick={() => onChange(values.filter(x => x !== v))} aria-label={`Remove ${v}`} style={chipRemoveStyle}>×</button>
        </span>
      ))}
      <input
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add() } }}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder={placeholder}
        style={mergeInputStyle}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// AutocompleteTagList — ESCO-powered skill entry
// ---------------------------------------------------------------------------

interface SkillsState {
  skills: string[]
  escoEntries: EscoEntry[]
}

interface AutocompleteTagListProps {
  state: SkillsState
  onChange: (s: SkillsState) => void
  lang?: 'en' | 'fr'
}

function AutocompleteTagList({ state, onChange, lang = 'en' }: AutocompleteTagListProps) {
  const { skills, escoEntries } = state
  const [draft, setDraft] = useState('')
  const [suggestions, setSuggestions] = useState<EscoSuggestion[]>([])
  const [activeIndex, setActiveIndex] = useState(-1)
  const [loading, setLoading] = useState(false)
  const [focused, setFocused] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // Close dropdown when clicking outside.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setSuggestions([])
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const fetchSuggestions = useCallback((q: string) => {
    if (!q.trim() || q.trim().length < 2) {
      setSuggestions([])
      return
    }
    setLoading(true)
    searchEscoSkills(q.trim(), lang, 8)
      .then(results => {
        // Filter out skills already added.
        setSuggestions(results.filter(r => !includesCaseInsensitive(skills, r.keyword)))
        setActiveIndex(-1)
      })
      .catch(() => setSuggestions([]))
      .finally(() => setLoading(false))
  }, [lang, skills])

  const onDraftChange = (value: string) => {
    setDraft(value)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchSuggestions(value), 300)
  }

  const addSuggestion = (suggestion: EscoSuggestion) => {
    const keyword = suggestion.keyword
    if (includesCaseInsensitive(skills, keyword)) return
    const entry: EscoEntry = {
      concept_uri: suggestion.concept_uri,
      label: suggestion.label,
      keyword,
      custom: false,
    }
    onChange({ skills: [...skills, keyword], escoEntries: [...escoEntries, entry] })
    setDraft('')
    setSuggestions([])
    setActiveIndex(-1)
  }

  const addCustom = () => {
    const trimmed = draft.trim()
    if (!trimmed || includesCaseInsensitive(skills, trimmed)) {
      setDraft('')
      setSuggestions([])
      return
    }
    const entry: EscoEntry = {
      concept_uri: null,
      label: trimmed,
      keyword: trimmed,
      custom: true,
    }
    onChange({ skills: [...skills, trimmed], escoEntries: [...escoEntries, entry] })
    setDraft('')
    setSuggestions([])
    setActiveIndex(-1)
  }

  const removeSkill = (keyword: string) => {
    onChange({
      skills: skills.filter(s => s !== keyword),
      escoEntries: escoEntries.filter(e => e.keyword !== keyword),
    })
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex(i => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex(i => Math.max(i - 1, -1))
    } else if (e.key === 'Escape') {
      setSuggestions([])
      setActiveIndex(-1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIndex >= 0 && activeIndex < suggestions.length) {
        addSuggestion(suggestions[activeIndex])
      } else {
        // No suggestion selected — add as custom.
        addCustom()
      }
    }
  }

  // Lookup the ESCO entry for a skill keyword (for badge display).
  const entryFor = (keyword: string) =>
    escoEntries.find(e => e.keyword === keyword)

  return (
    <div ref={containerRef}>
      <div style={mergeFieldStyle(focused)}>
        {skills.map(keyword => {
          const entry = entryFor(keyword)
          const isEsco = entry && !entry.custom && entry.concept_uri && !entry.concept_uri.startsWith('local:')
          return (
            <span key={keyword} style={{
              background: 'var(--color-surface-elevated)',
              border: `1px solid ${isEsco ? 'var(--color-accent)66' : 'var(--color-border)'}`,
              borderRadius: '7px',
              padding: '0.35rem 0.45rem 0.35rem 0.8rem',
              fontSize: '0.86rem',
              display: 'flex',
              alignItems: 'center',
              gap: '0.5rem',
            }}>
              {keyword}
              {isEsco && (
                <span title="Matched to ESCO taxonomy" style={{
                  fontSize: '0.68rem',
                  background: 'var(--color-accent)',
                  color: '#fff',
                  borderRadius: '10px',
                  padding: '0.05rem 0.35rem',
                  fontWeight: 700,
                  lineHeight: '1.4',
                }}>ESCO</span>
              )}
              <button onClick={() => removeSkill(keyword)} aria-label={`Remove ${keyword}`} style={chipRemoveStyle}>×</button>
            </span>
          )
        })}
        <input
          value={draft}
          onChange={e => onDraftChange(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder="Search ESCO skills, or press Enter to add custom…"
          style={mergeInputStyle}
        />
        {loading && <span style={{ fontSize: '0.78rem', color: 'var(--color-muted)', flex: 'none' }}>…</span>}

        {suggestions.length > 0 && (
          <div style={dropdownStyle}>
            {suggestions.map((s, i) => (
              <div
                key={s.concept_uri}
                onMouseDown={e => { e.preventDefault(); addSuggestion(s) }}
                onMouseEnter={() => setActiveIndex(i)}
                style={{
                  padding: '0.6rem 0.85rem',
                  cursor: 'pointer',
                  background: i === activeIndex ? 'var(--color-accent)' : 'transparent',
                  color: i === activeIndex ? '#fff' : 'var(--color-text)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: '0.5rem',
                  fontSize: '0.88rem',
                }}
              >
                <span style={{ flex: 1 }}>
                  <strong>{s.keyword}</strong>
                  {s.keyword !== s.label && (
                    <span style={{
                      fontSize: '0.78rem',
                      color: i === activeIndex ? 'rgba(255,255,255,0.75)' : 'var(--color-muted)',
                      marginLeft: '0.45rem',
                    }}>
                      {s.label}
                    </span>
                  )}
                </span>
                {s.is_esco && (
                  <span style={{
                    fontSize: '0.68rem',
                    background: i === activeIndex ? 'rgba(255,255,255,0.25)' : 'var(--color-accent)',
                    color: '#fff',
                    borderRadius: '10px',
                    padding: '0.05rem 0.4rem',
                    fontWeight: 700,
                    whiteSpace: 'nowrap',
                    lineHeight: '1.5',
                  }}>ESCO</span>
                )}
              </div>
            ))}
            <div style={{
              padding: '0.4rem 0.85rem',
              fontSize: '0.78rem',
              color: 'var(--color-muted)',
              borderTop: '1px solid var(--color-border)',
              fontStyle: 'italic',
            }}>
              Press Enter to add &ldquo;{draft.trim()}&rdquo; as a custom skill
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// DomainPicker — search-and-select, same shape as the skills
// AutocompleteTagList: only selected domains are shown, as removable chips;
// nothing unselected is displayed. Two sources feed the dropdown as you type:
//
//   1. CURATED_DOMAINS — a small set of IT/common-category keys that have a
//      hand-tuned keyword list in config/filters.yaml's `domain_keywords`
//      (many synonyms in English/French — see src/pipeline/filter.py).
//      Matched locally against the typed text, no network call.
//   2. The full ESCO occupation index (/api/esco/occupations) — covers any
//      profession, e.g. "seller" or "cleaner", that isn't one of the curated
//      keys above.
//
// Picking a curated match stores its canonical key (e.g. "software_development")
// so it gets that rich keyword list; picking an ESCO match or typing custom
// text stores it verbatim and is used as a literal title keyword instead
// (same fallback target_skills already get — see build_filter_config()).
// Either way, this stays a pure search box: nothing is pinned/always-visible.
// ---------------------------------------------------------------------------

const CURATED_DOMAINS: { value: string; label: string }[] = [
  { value: 'software_development', label: 'Software Development' },
  { value: 'web_development', label: 'Web Development' },
  { value: 'qa_testing', label: 'QA & Testing' },
  { value: 'automation', label: 'Automation' },
  { value: 'data', label: 'Data & AI' },
  { value: 'it_support', label: 'IT Support' },
  { value: 'systems', label: 'Systems' },
  { value: 'networking', label: 'Networking' },
  { value: 'cybersecurity', label: 'Cybersecurity' },
  { value: 'devops', label: 'DevOps' },
  { value: 'cloud', label: 'Cloud' },
  { value: 'sales', label: 'Sales' },
  { value: 'cleaning', label: 'Cleaning' },
  { value: 'healthcare', label: 'Healthcare' },
  { value: 'accounting', label: 'Accounting & Finance' },
  { value: 'legal', label: 'Legal' },
  { value: 'trades', label: 'Skilled Trades' },
  { value: 'manual_labor', label: 'Manual Labor & Warehouse' },
  { value: 'hospitality', label: 'Hospitality' },
  { value: 'logistics', label: 'Logistics & Transport' },
  { value: 'recruitment', label: 'HR & Recruitment' },
  { value: 'procurement', label: 'Procurement' },
  { value: 'marketing', label: 'Marketing' },
]

const DOMAIN_LABELS: Record<string, string> = Object.fromEntries(CURATED_DOMAINS.map(d => [d.value, d.label]))

interface DomainSuggestion {
  value: string
  label: string
  badge: 'Curated' | 'ESCO' | null
}

function DomainPicker({ values, onChange }: { values: string[]; onChange: (v: string[]) => void }) {
  const [query, setQuery] = useState('')
  const trimmedQuery = query.trim()
  const [escoResults, setEscoResults] = useState<EscoSuggestion[]>([])
  const [escoLoading, setEscoLoading] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [focused, setFocused] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setEscoResults([])
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (trimmedQuery.length < 2) {
      setEscoResults([])
      return
    }
    if (debounceRef.current) clearTimeout(debounceRef.current)
    setEscoLoading(true)
    debounceRef.current = setTimeout(() => {
      searchEscoOccupations(trimmedQuery, 'en', 8)
        .then(results => {
          setEscoResults(results.filter(r => !includesCaseInsensitive(values, r.keyword)))
          setActiveIndex(-1)
        })
        .catch(() => setEscoResults([]))
        .finally(() => setEscoLoading(false))
    }, 300)
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trimmedQuery])

  const curatedMatches: DomainSuggestion[] = trimmedQuery.length >= 1
    ? CURATED_DOMAINS
        .filter(d => d.label.toLowerCase().includes(trimmedQuery.toLowerCase()) && !values.includes(d.value))
        .map(d => ({ value: d.value, label: d.label, badge: 'Curated' as const }))
    : []

  // Skip an ESCO result whose label is basically the same word as a curated
  // match already shown, to avoid near-duplicate rows (e.g. curated
  // "Software Development" next to ESCO "software development" verbatim).
  const curatedLabelsLower = new Set(curatedMatches.map(c => c.label.toLowerCase()))
  const escoMatches: DomainSuggestion[] = escoResults
    .filter(r => !curatedLabelsLower.has(r.label.toLowerCase()))
    .map(r => ({ value: r.keyword, label: r.label, badge: r.is_esco ? ('ESCO' as const) : null }))

  const suggestions = [...curatedMatches, ...escoMatches].slice(0, 10)

  const addValue = (v: string) => {
    if (!includesCaseInsensitive(values, v)) onChange([...values, v])
    setQuery('')
    setEscoResults([])
    setActiveIndex(-1)
  }

  const removeValue = (v: string) => onChange(values.filter(x => x !== v))

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex(i => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex(i => Math.max(i - 1, -1))
    } else if (e.key === 'Escape') {
      setEscoResults([])
      setActiveIndex(-1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIndex >= 0 && activeIndex < suggestions.length) {
        addValue(suggestions[activeIndex].value)
      } else if (trimmedQuery) {
        addValue(trimmedQuery)
      }
    }
  }

  return (
    <div ref={containerRef}>
      <div style={mergeFieldStyle(focused)}>
        {values.map(v => (
          <span key={v} style={{
            background: 'var(--color-surface-elevated)',
            border: `1px solid ${DOMAIN_LABELS[v] ? 'var(--color-accent)66' : 'var(--color-border)'}`,
            borderRadius: '7px',
            padding: '0.35rem 0.45rem 0.35rem 0.8rem',
            fontSize: '0.86rem',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}>
            {DOMAIN_LABELS[v] ?? v}
            <button onClick={() => removeValue(v)} aria-label={`Remove ${v}`} style={chipRemoveStyle}>×</button>
          </span>
        ))}
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder="Search any domain or occupation (e.g. software development, seller, cleaner)…"
          style={mergeInputStyle}
        />
        {escoLoading && <span style={{ fontSize: '0.78rem', color: 'var(--color-muted)', flex: 'none' }}>…</span>}

        {trimmedQuery.length >= 1 && suggestions.length > 0 && (
          <div style={dropdownStyle}>
            {suggestions.map((s, i) => (
              <div
                key={`${s.badge ?? 'plain'}-${s.value}`}
                onMouseDown={e => { e.preventDefault(); addValue(s.value) }}
                onMouseEnter={() => setActiveIndex(i)}
                style={{
                  padding: '0.6rem 0.85rem',
                  cursor: 'pointer',
                  background: i === activeIndex ? 'var(--color-accent)' : 'transparent',
                  color: i === activeIndex ? '#fff' : 'var(--color-text)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: '0.5rem',
                  fontSize: '0.88rem',
                }}
              >
                <span>{s.label}</span>
                {s.badge && (
                  <span style={{
                    fontSize: '0.68rem',
                    background: i === activeIndex ? 'rgba(255,255,255,0.25)' : (s.badge === 'Curated' ? 'var(--color-info)' : 'var(--color-accent)'),
                    color: '#fff',
                    borderRadius: '10px',
                    padding: '0.05rem 0.4rem',
                    fontWeight: 700,
                    whiteSpace: 'nowrap',
                  }}>{s.badge}</span>
                )}
              </div>
            ))}
            <div style={{
              padding: '0.4rem 0.85rem',
              fontSize: '0.78rem',
              color: 'var(--color-muted)',
              borderTop: '1px solid var(--color-border)',
              fontStyle: 'italic',
            }}>
              No match? Press Enter to add &ldquo;{trimmedQuery}&rdquo; as a custom domain
            </div>
          </div>
        )}

        {trimmedQuery.length >= 2 && !escoLoading && suggestions.length === 0 && (
          <div style={{ position: 'absolute', top: 'calc(100% + 6px)', left: 0, right: 0, fontSize: '0.82rem', color: 'var(--color-muted)' }}>
            No match for &ldquo;{trimmedQuery}&rdquo; — press Enter to add it as a custom domain.
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// LocationPreferencesEditor — ordered department/remote priority list
//
// Backs profile.geography.location_preferences: the SOLE source of truth
// for location_fit scoring's priority tiers (src/pipeline/scoring.py) —
// a RANKING preference, never a hard filter, except for an explicit
// "Excluded" entry which vetoes only that one location (see
// src/pipeline/location_match.py). List order IS priority — there is no
// separate numeric field, reordering with the ↑/↓ buttons below is
// reprioritizing. Reuses the same getAllDepartments() list already used by
// the Jobs page's department filter (fetched once, filtered locally).
// ---------------------------------------------------------------------------

const STATUS_LABEL: Record<LocationPreference['status'], string> = {
  preferred: 'Preferred',
  acceptable: 'Acceptable',
  excluded: 'Excluded',
}

const iconButtonStyle: React.CSSProperties = {
  background: 'var(--color-bg)',
  border: '1px solid var(--color-border)',
  borderRadius: '6px',
  width: '1.8rem',
  height: '1.8rem',
  fontSize: '0.85rem',
  color: 'var(--color-text)',
  cursor: 'pointer',
  flexShrink: 0,
}

function locationPreferenceLabel(p: LocationPreference, byCode: Map<string, FrenchDepartment>): string {
  if (p.label) return p.label
  if (p.type === 'department') {
    const name = byCode.get(p.value)?.name
    return name ? `${name} (${p.value})` : p.value
  }
  return 'Remote France'
}

function LocationPreferencesEditor({ values, onChange }: {
  values: LocationPreference[]
  onChange: (v: LocationPreference[]) => void
}) {
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(-1)
  const [focused, setFocused] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const { data: allDepartments } = useQuery({
    queryKey: ['departments-all'],
    queryFn: getAllDepartments,
    staleTime: Infinity,
  })
  const departments = allDepartments ?? []
  const byCode = new Map(departments.map(d => [d.code, d]))
  const usedDeptCodes = new Set(values.filter(v => v.type === 'department').map(v => v.value))
  const hasRemote = values.some(v => v.type === 'remote')

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const trimmedQuery = query.trim().toLowerCase()
  const suggestions: FrenchDepartment[] = trimmedQuery.length >= 1
    ? departments
        .filter(d => !usedDeptCodes.has(d.code))
        .filter(d => d.code.toLowerCase().includes(trimmedQuery) || d.name.toLowerCase().includes(trimmedQuery))
        .slice(0, 10)
    : []

  const addDepartment = (code: string) => {
    if (!usedDeptCodes.has(code)) {
      onChange([...values, { type: 'department', value: code, status: 'preferred' }])
    }
    setQuery('')
    setActiveIndex(-1)
  }

  const addRemote = () => {
    if (!hasRemote) {
      onChange([...values, { type: 'remote', value: 'remote_france', status: 'preferred', label: 'Remote France' }])
    }
  }

  const removeAt = (idx: number) => onChange(values.filter((_, i) => i !== idx))

  const moveBy = (idx: number, delta: number) => {
    const target = idx + delta
    if (target < 0 || target >= values.length) return
    const next = [...values]
    ;[next[idx], next[target]] = [next[target], next[idx]]
    onChange(next)
  }

  const setStatus = (idx: number, status: LocationPreference['status']) => {
    const next = [...values]
    next[idx] = { ...next[idx], status }
    onChange(next)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex(i => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex(i => Math.max(i - 1, -1))
    } else if (e.key === 'Escape') {
      setActiveIndex(-1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIndex >= 0 && activeIndex < suggestions.length) {
        addDepartment(suggestions[activeIndex].code)
      } else if (suggestions.length === 1) {
        addDepartment(suggestions[0].code)
      }
    }
  }

  return (
    <div ref={containerRef}>
      {values.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginBottom: '0.75rem' }}>
          {values.map((p, idx) => (
            <div
              key={`${p.type}-${p.value}`}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5rem',
                background: 'var(--color-surface-elevated)',
                border: '1px solid var(--color-border)',
                borderRadius: '7px',
                padding: '0.5rem 0.7rem',
              }}
            >
              <span style={{ fontSize: '0.8rem', color: 'var(--color-muted)', width: '1.3rem', textAlign: 'center', flexShrink: 0 }}>
                {idx + 1}
              </span>
              <span style={{ fontSize: '1rem', flexShrink: 0 }}>{p.type === 'remote' ? '🌍' : '📍'}</span>
              <span style={{ flex: 1, fontSize: '0.88rem', minWidth: 0 }}>{locationPreferenceLabel(p, byCode)}</span>
              <select
                value={p.status}
                onChange={e => setStatus(idx, e.target.value as LocationPreference['status'])}
                style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)', borderRadius: '6px', padding: '0.3rem 0.5rem', fontSize: '0.8rem', color: 'var(--color-text)' }}
              >
                {(Object.keys(STATUS_LABEL) as LocationPreference['status'][]).map(s => (
                  <option key={s} value={s}>{STATUS_LABEL[s]}</option>
                ))}
              </select>
              <button onClick={() => moveBy(idx, -1)} disabled={idx === 0} aria-label="Move up" style={iconButtonStyle}>↑</button>
              <button onClick={() => moveBy(idx, 1)} disabled={idx === values.length - 1} aria-label="Move down" style={iconButtonStyle}>↓</button>
              <button onClick={() => removeAt(idx)} aria-label={`Remove ${locationPreferenceLabel(p, byCode)}`} style={chipRemoveStyle}>×</button>
            </div>
          ))}
        </div>
      )}

      <div style={mergeFieldStyle(focused)}>
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder="Search a department to add (e.g. 83 or Var)…"
          style={mergeInputStyle}
        />

        {suggestions.length > 0 && (
          <div style={dropdownStyle}>
            {suggestions.map((d, i) => (
              <div
                key={d.code}
                onMouseDown={e => { e.preventDefault(); addDepartment(d.code) }}
                onMouseEnter={() => setActiveIndex(i)}
                style={{
                  padding: '0.6rem 0.85rem',
                  cursor: 'pointer',
                  background: i === activeIndex ? 'var(--color-accent)' : 'transparent',
                  color: i === activeIndex ? '#fff' : 'var(--color-text)',
                  fontSize: '0.88rem',
                }}
              >
                <strong>{d.code}</strong> — {d.name}
              </div>
            ))}
          </div>
        )}

        {trimmedQuery.length >= 1 && suggestions.length === 0 && (
          <div style={{ position: 'absolute', top: 'calc(100% + 6px)', left: 0, right: 0, fontSize: '0.82rem', color: 'var(--color-muted)' }}>
            No department matches &ldquo;{query.trim()}&rdquo;.
          </div>
        )}
      </div>

      <button
        onClick={addRemote}
        disabled={hasRemote}
        style={{
          marginTop: '0.6rem',
          background: 'var(--color-bg)',
          border: '1px solid var(--color-border)',
          borderRadius: '7px',
          padding: '0.45rem 0.9rem',
          fontSize: '0.85rem',
          color: hasRemote ? 'var(--color-muted)' : 'var(--color-text)',
          cursor: hasRemote ? 'default' : 'pointer',
        }}
      >
        🌍 {hasRemote ? 'Remote France added' : 'Add Remote France'}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// CheckboxGroup
// ---------------------------------------------------------------------------

function CheckboxGroup({ options, values, onChange }: { options: string[]; values: string[]; onChange: (v: string[]) => void }) {
  const toggle = (opt: string) => {
    if (values.includes(opt)) onChange(values.filter(x => x !== opt))
    else onChange([...values, opt])
  }
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.55rem' }}>
      {options.map(opt => (
        <button
          key={opt}
          onClick={() => toggle(opt)}
          style={{
            background: values.includes(opt) ? 'var(--color-accent)' : 'var(--color-bg)',
            border: `1px solid ${values.includes(opt) ? 'var(--color-accent)' : 'var(--color-border)'}`,
            borderRadius: '20px',
            padding: '0.4rem 1.05rem',
            fontSize: '0.87rem',
            color: values.includes(opt) ? '#fff' : 'var(--color-muted)',
            cursor: 'pointer',
            transition: 'all 0.15s',
          }}
        >
          {opt}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SegmentedControl — single-select over a small fixed set of options
// ---------------------------------------------------------------------------

function SegmentedControl<T extends string>({ options, value, onChange }: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div style={{ display: 'inline-flex', border: '1px solid var(--color-border)', borderRadius: '20px', padding: '0.2rem', gap: '0.2rem' }}>
      {options.map(opt => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          style={{
            background: value === opt.value ? 'var(--color-accent)' : 'transparent',
            border: 'none',
            borderRadius: '16px',
            padding: '0.42rem 1.05rem',
            fontSize: '0.86rem',
            color: value === opt.value ? '#fff' : 'var(--color-muted)',
            cursor: 'pointer',
            fontWeight: value === opt.value ? 600 : 400,
            transition: 'all 0.15s',
          }}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
const EDUCATION_LEVELS = ['bac', 'bac+1', 'bac+2', 'bac+3', 'bac+4', 'bac+5', 'bac+8']

// ---------------------------------------------------------------------------
// ProfilePage
// ---------------------------------------------------------------------------

export default function ProfilePage() {
  const queryClient = useQueryClient()

  const { data: serverProfile, isLoading, error } = useQuery({
    queryKey: ['profile'],
    queryFn: getProfile,
  })

  const [profile, setProfile] = useState<Profile | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (serverProfile && !profile) setProfile(serverProfile)
  }, [serverProfile, profile])

  const { mutate: save, isPending, error: saveError } = useMutation({
    mutationFn: (p: Profile) => updateProfile(p),
    onSuccess: (updated) => {
      setProfile(updated)
      queryClient.invalidateQueries({ queryKey: ['profile'] })
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    },
  })

  if (isLoading) return <LoadingState />
  if (error) return <div style={{ color: 'var(--color-red)', padding: '2rem' }}>Could not load profile.</div>
  if (!profile) return null

  const set = <K extends keyof Profile>(key: K, value: Profile[K]) =>
    setProfile(p => p ? { ...p, [key]: value } : p)

  const setGeo = <K extends keyof Profile['geography']>(key: K, value: Profile['geography'][K]) =>
    setProfile(p => p ? { ...p, geography: { ...p.geography, [key]: value } } : p)

  const setExp = <K extends keyof Profile['experience']>(key: K, value: Profile['experience'][K]) =>
    setProfile(p => p ? { ...p, experience: { ...p.experience, [key]: value } } : p)

  const skillsState: SkillsState = {
    skills: profile.target_skills,
    escoEntries: profile.target_skills_esco ?? [],
  }

  const setSkillsState = (s: SkillsState) =>
    setProfile(p => p ? { ...p, target_skills: s.skills, target_skills_esco: s.escoEntries } : p)

  const workModeOptions = ['remote', 'hybrid', 'on-site']
  const contractOptions = ['full-time', 'part-time', 'internship', 'freelance']
  const seniorityOptions = ['junior', 'mid', 'senior', 'lead', 'director', 'executive']
  const levelOptions = ['junior', 'mid', 'senior', 'lead']

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem', flexWrap: 'wrap', gap: '0.75rem' }}>
        <h1 style={{ margin: 0, fontSize: '1.4rem', fontWeight: 700 }}>Profile</h1>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {saved && <span style={{ color: 'var(--color-green)', fontSize: '0.85rem' }}>Saved!</span>}
          {saveError && <span style={{ color: 'var(--color-red)', fontSize: '0.85rem' }}>Save failed.</span>}
          <button
            onClick={() => save(profile)}
            disabled={isPending}
            style={{
              background: isPending ? 'var(--color-border)' : 'var(--color-accent)',
              color: '#fff',
              border: 'none',
              borderRadius: '7px',
              padding: '0.45rem 1.1rem',
              fontSize: '0.88rem',
              fontWeight: 600,
              cursor: isPending ? 'not-allowed' : 'pointer',
            }}
          >
            {isPending ? 'Saving…' : 'Save profile'}
          </button>
        </div>
      </div>

      <Card style={{ marginBottom: '1rem' }} title="Target domains & skills" subtitle="What the pipeline searches for and matches jobs against">
        <div style={{ marginTop: 'var(--space-4)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem', marginBottom: '0.5rem' }}>
            <span style={{ ...label, marginBottom: 0 }}>Target domains<CountBadge count={profile.target_domains.length} /></span>
            <Switch
              checked={profile.focus_domain}
              onChange={v => set('focus_domain', v)}
              label={profile.focus_domain ? 'Focus on domain' : 'Focus on skills'}
            />
          </div>
          <DomainPicker values={profile.target_domains} onChange={v => set('target_domains', v)} />
        </div>

        <hr style={sectionDivider} />

        <div>
          <span style={label}>Target skills<CountBadge count={profile.target_skills.length} /></span>
          <AutocompleteTagList state={skillsState} onChange={setSkillsState} />
        </div>

        <hr style={sectionDivider} />

        <div>
          <span style={label}>Languages<CountBadge count={profile.languages.length} /></span>
          <TagList values={profile.languages} onChange={v => set('languages', v)} tone="language" placeholder="Add a language and press Enter…" />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title="Geography">
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>Preferred countries</span>
          <TagList values={profile.geography.preferred_countries} onChange={v => setGeo('preferred_countries', v)} placeholder="Add a country and press Enter…" />
        </div>

        <div style={fieldGroup}>
          <span style={label}>Remote scope</span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            How far outside your preferred countries a remote job may be based
          </div>
          <SegmentedControl
            value={profile.geography.allowed_regions.includes('Europe') ? 'europe' : 'same_country'}
            onChange={v => setGeo('allowed_regions', v === 'europe' ? ['Europe'] : [])}
            options={[
              { value: 'same_country', label: 'Same country only' },
              { value: 'europe', label: 'Europe' },
            ]}
          />
        </div>

        <div style={fieldGroup}>
          <span style={label}>
            Location priorities<CountBadge count={profile.geography.location_preferences.length} />
          </span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            Departments and/or remote work you'd especially like, ordered by priority (drag with
            ↑/↓ — position #1 is your strongest preference). This drives job ranking.
            "Acceptable" ranks lower than "Preferred"; "Excluded" stops a location from earning
            a priority boost. None of these is a hard filter: jobs outside this list (including
            other departments and remote) remain fully eligible and can still rank highly on
            domain, skills, and other factors.
          </div>
          <LocationPreferencesEditor
            values={profile.geography.location_preferences}
            onChange={v => setGeo('location_preferences', v)}
          />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title="Work preferences">
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>Work modes</span>
          <CheckboxGroup options={workModeOptions} values={profile.work_modes} onChange={v => set('work_modes', v)} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>Contract types</span>
          <CheckboxGroup options={contractOptions} values={profile.contract_types} onChange={v => set('contract_types', v)} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>Freshness (days)</span>
          <input
            type="number"
            value={profile.freshness_days}
            min={1}
            max={365}
            onChange={e => set('freshness_days', parseInt(e.target.value) || 30)}
            style={{ ...inputStyle, width: '100px' }}
          />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title="Experience">
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>Preferred levels</span>
          <CheckboxGroup options={levelOptions} values={profile.experience.preferred_levels} onChange={v => setExp('preferred_levels', v)} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>Excluded seniority keywords</span>
          <CheckboxGroup options={seniorityOptions} values={profile.experience.excluded_seniority} onChange={v => setExp('excluded_seniority', v)} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>Max years of experience (optional)</span>
          <input
            type="number"
            value={profile.experience.max_years ?? ''}
            min={0}
            max={50}
            placeholder="No limit"
            onChange={e => setExp('max_years', e.target.value === '' ? null : parseInt(e.target.value))}
            style={{ ...inputStyle, width: '120px' }}
          />
        </div>
      </Card>

      <Card title="Education">
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>Maximum education level (Bac+N)</span>
          <select
            value={profile.max_education ?? ''}
            onChange={e => set('max_education', e.target.value === '' ? null : e.target.value)}
            style={{ ...inputStyle, width: '160px' }}
          >
            <option value="">No limit</option>
            {EDUCATION_LEVELS.map(lvl => (
              <option key={lvl} value={lvl}>{lvl}</option>
            ))}
          </select>
        </div>
      </Card>
    </div>
  )
}
