import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getProfile, updateProfile, searchEscoSkills, searchEscoOccupations,
  getAllDepartments, searchPlaces,
  type Profile, type EscoEntry, type EscoSuggestion, type FrenchDepartment, type LocationPreference,
  type HomePlace, type PlaceSuggestion, type RemotePolicy,
} from '../api/client'
import { useTranslation } from 'react-i18next'
import { Card, LoadingState, Switch } from '../components'
import i18n from '../i18n'

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
  language: { border: 'color-mix(in srgb, var(--color-info) 40%, transparent)' },
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
    boxShadow: focused ? '0 0 0 3px color-mix(in srgb, var(--color-accent) 13%, transparent)' : undefined,
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
  boxShadow: 'var(--shadow-lg)',
  zIndex: 100,
  overflow: 'hidden',
}

// ---------------------------------------------------------------------------
// TagList — plain comma / Enter tag entry (for languages, preferred countries)
// ---------------------------------------------------------------------------

function TagList({ values, onChange, tone = 'default', placeholder }: {
  values: string[]; onChange: (v: string[]) => void; tone?: TagTone; placeholder?: string
}) {
  const { t } = useTranslation()
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
          <button onClick={() => onChange(values.filter(x => x !== v))} aria-label={t('profile.remove', { item: v })} style={chipRemoveStyle}>×</button>
        </span>
      ))}
      <input
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add() } }}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder={placeholder ?? t('profile.addPlaceholder')}
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
  const { t } = useTranslation()
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
              border: `1px solid ${isEsco ? 'color-mix(in srgb, var(--color-accent) 40%, transparent)' : 'var(--color-border)'}`,
              borderRadius: '7px',
              padding: '0.35rem 0.45rem 0.35rem 0.8rem',
              fontSize: '0.86rem',
              display: 'flex',
              alignItems: 'center',
              gap: '0.5rem',
            }}>
              {keyword}
              {isEsco && (
                <span title={t('profile.skills.escoTitle')} style={{
                  fontSize: '0.68rem',
                  background: 'var(--color-accent)',
                  color: 'var(--color-on-accent)',
                  borderRadius: '10px',
                  padding: '0.05rem 0.35rem',
                  fontWeight: 700,
                  lineHeight: '1.4',
                }}>ESCO</span>
              )}
              <button onClick={() => removeSkill(keyword)} aria-label={t('profile.remove', { item: keyword })} style={chipRemoveStyle}>×</button>
            </span>
          )
        })}
        <input
          value={draft}
          onChange={e => onDraftChange(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={t('profile.skills.placeholder')}
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
                  color: i === activeIndex ? 'var(--color-on-accent)' : 'var(--color-text)',
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
                      color: i === activeIndex ? 'color-mix(in srgb, var(--color-on-accent) 80%, transparent)' : 'var(--color-muted)',
                      marginLeft: '0.45rem',
                    }}>
                      {s.label}
                    </span>
                  )}
                </span>
                {s.is_esco && (
                  <span style={{
                    fontSize: '0.68rem',
                    background: i === activeIndex ? 'color-mix(in srgb, var(--color-on-accent) 25%, transparent)' : 'var(--color-accent)',
                    color: 'var(--color-on-accent)',
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
              {t('profile.skills.addCustom', { value: draft.trim() })}
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

// Labels: profile.domains.labels.<value>
const CURATED_DOMAIN_VALUES = [
  'software_development',
  'web_development',
  'qa_testing',
  'automation',
  'data',
  'it_support',
  'systems',
  'networking',
  'cybersecurity',
  'devops',
  'cloud',
  'sales',
  'cleaning',
  'healthcare',
  'accounting',
  'legal',
  'trades',
  'manual_labor',
  'hospitality',
  'logistics',
  'recruitment',
  'procurement',
  'marketing',
]

const CURATED_DOMAINS = new Set(CURATED_DOMAIN_VALUES)
const domainLabel = (value: string): string | undefined =>
  CURATED_DOMAINS.has(value) ? i18n.t(`profile.domains.labels.${value}`) : undefined

interface DomainSuggestion {
  value: string
  label: string
  badge: 'Curated' | 'ESCO' | null
}

function DomainPicker({ values, onChange }: { values: string[]; onChange: (v: string[]) => void }) {
  const { t } = useTranslation()
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
    ? CURATED_DOMAIN_VALUES
        .map(value => ({ value, label: domainLabel(value)! }))
        .filter(d => d.label.toLowerCase().includes(trimmedQuery.toLowerCase()) && !values.includes(d.value))
        .map(d => ({ ...d, badge: 'Curated' as const }))
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
            border: `1px solid ${domainLabel(v) ? 'color-mix(in srgb, var(--color-accent) 40%, transparent)' : 'var(--color-border)'}`,
            borderRadius: '7px',
            padding: '0.35rem 0.45rem 0.35rem 0.8rem',
            fontSize: '0.86rem',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}>
            {domainLabel(v) ?? v}
            <button onClick={() => removeValue(v)} aria-label={t('profile.remove', { item: v })} style={chipRemoveStyle}>×</button>
          </span>
        ))}
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={t('profile.domains.placeholder')}
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
                  color: i === activeIndex ? 'var(--color-on-accent)' : 'var(--color-text)',
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
                    background: i === activeIndex ? 'color-mix(in srgb, var(--color-on-accent) 25%, transparent)' : (s.badge === 'Curated' ? 'var(--color-info)' : 'var(--color-accent)'),
                    color: 'var(--color-on-accent)',
                    borderRadius: '10px',
                    padding: '0.05rem 0.4rem',
                    fontWeight: 700,
                    whiteSpace: 'nowrap',
                  }}>{s.badge === 'Curated' ? t('profile.domains.curated') : s.badge}</span>
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
              {t('profile.domains.addCustomHint', { value: trimmedQuery })}
            </div>
          </div>
        )}

        {trimmedQuery.length >= 2 && !escoLoading && suggestions.length === 0 && (
          <div style={{ position: 'absolute', top: 'calc(100% + 6px)', left: 0, right: 0, fontSize: '0.82rem', color: 'var(--color-muted)' }}>
            {t('profile.domains.noMatch', { value: trimmedQuery })}
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

// Labels: profile.location.status.<status>
const LOCATION_STATUSES: LocationPreference['status'][] = ['preferred', 'acceptable', 'excluded']

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
  // The remote entry is stored with an English label; show it in the UI language.
  if (p.type === 'remote') return i18n.t('profile.location.remoteFrance')
  if (p.label) return p.label
  if (p.type === 'department') {
    const name = byCode.get(p.value)?.name
    return name ? `${name} (${p.value})` : p.value
  }
  return i18n.t('profile.location.remoteFrance')
}

function LocationPreferencesEditor({ values, onChange }: {
  values: LocationPreference[]
  onChange: (v: LocationPreference[]) => void
}) {
  const { t } = useTranslation()
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
                {LOCATION_STATUSES.map(s => (
                  <option key={s} value={s}>{t(`profile.location.status.${s}`)}</option>
                ))}
              </select>
              <button onClick={() => moveBy(idx, -1)} disabled={idx === 0} aria-label={t('profile.location.moveUp')} style={iconButtonStyle}>↑</button>
              <button onClick={() => moveBy(idx, 1)} disabled={idx === values.length - 1} aria-label={t('profile.location.moveDown')} style={iconButtonStyle}>↓</button>
              <button onClick={() => removeAt(idx)} aria-label={t('profile.remove', { item: locationPreferenceLabel(p, byCode) })} style={chipRemoveStyle}>×</button>
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
          placeholder={t('profile.location.placeholder')}
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
                  color: i === activeIndex ? 'var(--color-on-accent)' : 'var(--color-text)',
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
            {t('profile.location.noMatch', { value: query.trim() })}
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
        {hasRemote ? t('profile.location.remoteAdded') : t('profile.location.addRemote')}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// CheckboxGroup
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// HomePicker — offline commune autocomplete for profile.geography.home
// ---------------------------------------------------------------------------

function HomePicker({ value, onChange }: { value: HomePlace | null; onChange: (v: HomePlace | null) => void }) {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<PlaceSuggestion[]>([])
  const searchable = query.trim().length >= 2
  const shown = searchable ? results : []
  useEffect(() => {
    if (!searchable) return
    let cancelled = false
    const timer = setTimeout(() => {
      searchPlaces(query.trim()).then(r => { if (!cancelled) setResults(r) }).catch(() => { if (!cancelled) setResults([]) })
    }, 200)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [query, searchable])
  const pick = (p: PlaceSuggestion) => {
    onChange({ name: p.name, insee: p.insee, lat: p.lat, lon: p.lon, department: p.department, precision: 'commune' })
    setQuery('')
    setResults([])
  }
  return (
    <div>
      <div style={{ fontSize: '0.85rem', marginBottom: '0.5rem' }}>
        {value
          ? <>{value.name}{value.department ? ` (${value.department})` : ''}
              {value.precision === 'department' && (
                <span style={{ color: 'var(--color-muted)' }}> — {t('profile.fields.homeDepartmentCentre')}</span>
              )}
              <button
                type="button"
                onClick={() => onChange(null)}
                style={{ marginLeft: '0.6rem', background: 'none', border: '1px solid var(--color-border)',
                         borderRadius: '6px', padding: '0.1rem 0.5rem', color: 'var(--color-muted)',
                         cursor: 'pointer', fontSize: '0.8rem' }}
              >
                {t('profile.fields.homeClear')}
              </button></>
          : <span style={{ color: 'var(--color-muted)' }}>{t('profile.fields.homeNotSet')}</span>}
      </div>
      <input
        style={inputStyle}
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder={t('profile.fields.homePlaceholder')}
        aria-label={t('profile.fields.home')}
      />
      {shown.length > 0 && (
        <div role="listbox" style={{ border: '1px solid var(--color-border)', borderRadius: '7px', marginTop: '0.3rem' }}>
          {shown.map(p => (
            <button
              key={p.insee}
              role="option"
              aria-selected={false}
              onClick={() => pick(p)}
              style={{ display: 'block', width: '100%', textAlign: 'left', background: 'none', border: 'none',
                       padding: '0.4rem 0.8rem', color: 'var(--color-text)', cursor: 'pointer', fontSize: '0.87rem' }}
            >
              {p.name} <span style={{ color: 'var(--color-muted)' }}>{p.department_name ?? p.department}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const REMOTE_POLICY_KEYS: (keyof RemotePolicy)[] = ['remote_france', 'remote_eu', 'remote_worldwide', 'hybrid']

// `labelFor` translates an option code; without it the code is shown as is, marked translate="no"
// (the excluded-seniority keywords are matched literally in job titles).
function CheckboxGroup({ options, values, onChange, labelFor }: {
  options: string[]; values: string[]; onChange: (v: string[]) => void; labelFor?: (opt: string) => string
}) {
  const toggle = (opt: string) => {
    if (values.includes(opt)) onChange(values.filter(x => x !== opt))
    else onChange([...values, opt])
  }
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.55rem' }}>
      {options.map(opt => (
        <button
          key={opt}
          translate={labelFor ? undefined : 'no'}
          onClick={() => toggle(opt)}
          style={{
            background: values.includes(opt) ? 'var(--color-accent)' : 'var(--color-bg)',
            border: `1px solid ${values.includes(opt) ? 'var(--color-accent)' : 'var(--color-border)'}`,
            borderRadius: '20px',
            padding: '0.4rem 1.05rem',
            fontSize: '0.87rem',
            color: values.includes(opt) ? 'var(--color-on-accent)' : 'var(--color-muted)',
            cursor: 'pointer',
            transition: 'all 0.15s',
          }}
        >
          {labelFor ? labelFor(opt) : opt}
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
            color: value === opt.value ? 'var(--color-on-accent)' : 'var(--color-muted)',
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
  const { t } = useTranslation()
  const optionLabel = (opt: string) => t(`profile.options.${opt}`)
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
  if (error) return <div style={{ color: 'var(--color-red)', padding: '2rem' }}>{t('profile.loadError')}</div>
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
        <h1 style={{ margin: 0, fontSize: '1.4rem', fontWeight: 700 }}>{t('profile.title')}</h1>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {saved && <span style={{ color: 'var(--color-green)', fontSize: '0.85rem' }}>{t('profile.saved')}</span>}
          {saveError && <span style={{ color: 'var(--color-red)', fontSize: '0.85rem' }}>{t('profile.saveFailed')}</span>}
          <button
            onClick={() => save(profile)}
            disabled={isPending}
            style={{
              background: isPending ? 'var(--color-border)' : 'var(--color-accent)',
              color: 'var(--color-on-accent)',
              border: 'none',
              borderRadius: '7px',
              padding: '0.45rem 1.1rem',
              fontSize: '0.88rem',
              fontWeight: 600,
              cursor: isPending ? 'not-allowed' : 'pointer',
            }}
          >
            {isPending ? t('profile.saving') : t('profile.save')}
          </button>
        </div>
      </div>

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.targets')} subtitle={t('profile.sections.targetsSubtitle')}>
        <div style={{ marginTop: 'var(--space-4)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem', marginBottom: '0.5rem' }}>
            <span style={{ ...label, marginBottom: 0 }}>{t('profile.fields.targetDomains')}<CountBadge count={profile.target_domains.length} /></span>
            <Switch
              checked={profile.focus_domain}
              onChange={v => set('focus_domain', v)}
              label={profile.focus_domain ? t('profile.fields.focusDomain') : t('profile.fields.focusSkills')}
            />
          </div>
          <DomainPicker values={profile.target_domains} onChange={v => set('target_domains', v)} />
        </div>

        <hr style={sectionDivider} />

        <div>
          <span style={label}>{t('profile.fields.targetSkills')}<CountBadge count={profile.target_skills.length} /></span>
          <AutocompleteTagList state={skillsState} onChange={setSkillsState} />
        </div>

        <hr style={sectionDivider} />

        <div>
          <span style={label}>{t('profile.fields.languages')}<CountBadge count={profile.languages.length} /></span>
          <TagList values={profile.languages} onChange={v => set('languages', v)} tone="language" placeholder={t('profile.fields.languagesPlaceholder')} />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.geography')}>
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.countries')}</span>
          <TagList values={profile.geography.preferred_countries} onChange={v => setGeo('preferred_countries', v)} placeholder={t('profile.fields.countriesPlaceholder')} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.remoteScope')}</span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            {t('profile.fields.remoteScopeHint')}
          </div>
          <SegmentedControl
            value={profile.geography.allowed_regions.includes('Europe') ? 'europe' : 'same_country'}
            onChange={v => setGeo('allowed_regions', v === 'europe' ? ['Europe'] : [])}
            options={[
              { value: 'same_country', label: t('profile.fields.sameCountry') },
              { value: 'europe', label: t('profile.fields.europe') },
            ]}
          />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.home')}</span>
          <HomePicker value={profile.geography.home} onChange={v => setGeo('home', v)} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.maxDistance', { km: profile.geography.max_distance_km })}</span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            {t('profile.fields.maxDistanceHint')}
          </div>
          <input
            type="range" min={5} max={300} step={5}
            value={profile.geography.max_distance_km}
            onChange={e => setGeo('max_distance_km', Number(e.target.value))}
            aria-label={t('profile.fields.maxDistance', { km: profile.geography.max_distance_km })}
            style={{ width: '100%' }}
          />
          <label style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', fontSize: '0.87rem', marginTop: '0.5rem' }}>
            <input
              type="checkbox"
              checked={profile.geography.strict_distance}
              onChange={e => setGeo('strict_distance', e.target.checked)}
            />
            {t('profile.fields.strictDistance')}
          </label>
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.remoteAccepted')}</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.9rem' }}>
            {REMOTE_POLICY_KEYS.map(key => (
              <label key={key} style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', fontSize: '0.87rem' }}>
                <input
                  type="checkbox"
                  checked={profile.geography.remote_policy[key]}
                  onChange={e => setGeo('remote_policy', { ...profile.geography.remote_policy, [key]: e.target.checked })}
                />
                {t(`profile.fields.remotePolicy.${key}`)}
              </label>
            ))}
          </div>
        </div>

        <div style={fieldGroup}>
          <span style={label}>
            {t('profile.fields.locationPriorities')}<CountBadge count={profile.geography.location_preferences.length} />
          </span>
          <div style={{ fontSize: '0.8rem', color: 'var(--color-muted)', marginBottom: '0.5rem' }}>
            {t('profile.fields.locationHomeNote')} {t('profile.fields.locationHint')}
          </div>
          <LocationPreferencesEditor
            values={profile.geography.location_preferences}
            onChange={v => setGeo('location_preferences', v)}
          />
        </div>
      </Card>

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.work')}>
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.workModes')}</span>
          <CheckboxGroup options={workModeOptions} values={profile.work_modes} onChange={v => set('work_modes', v)} labelFor={optionLabel} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.contractTypes')}</span>
          <CheckboxGroup options={contractOptions} values={profile.contract_types} onChange={v => set('contract_types', v)} labelFor={optionLabel} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.freshness')}</span>
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

      <Card style={{ marginBottom: '1rem' }} title={t('profile.sections.experience')}>
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.preferredLevels')}</span>
          <CheckboxGroup options={levelOptions} values={profile.experience.preferred_levels} onChange={v => setExp('preferred_levels', v)} labelFor={optionLabel} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.excludedSeniority')}</span>
          <CheckboxGroup options={seniorityOptions} values={profile.experience.excluded_seniority} onChange={v => setExp('excluded_seniority', v)} />
        </div>

        <div style={fieldGroup}>
          <span style={label}>{t('profile.fields.maxYears')}</span>
          <input
            type="number"
            value={profile.experience.max_years ?? ''}
            min={0}
            max={50}
            placeholder={t('profile.fields.noLimit')}
            onChange={e => setExp('max_years', e.target.value === '' ? null : parseInt(e.target.value))}
            style={{ ...inputStyle, width: '120px' }}
          />
        </div>
      </Card>

      <Card title={t('profile.sections.education')}>
        <div style={{ ...fieldGroup, marginTop: 'var(--space-4)' }}>
          <span style={label}>{t('profile.fields.maxEducation')}</span>
          <select
            value={profile.max_education ?? ''}
            onChange={e => set('max_education', e.target.value === '' ? null : e.target.value)}
            style={{ ...inputStyle, width: '160px' }}
          >
            <option value="">{t('profile.fields.noLimit')}</option>
            {EDUCATION_LEVELS.map(lvl => (
              <option key={lvl} value={lvl} translate="no">{lvl}</option>
            ))}
          </select>
        </div>
      </Card>
    </div>
  )
}
