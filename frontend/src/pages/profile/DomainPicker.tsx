import { useState, useEffect, useRef } from 'react'
import { searchEscoOccupations, type EscoSuggestion } from '../../api/client'
import { useTranslation } from 'react-i18next'
import i18n from '../../i18n'
import { chipRemoveStyle, dropdownStyle, includesCaseInsensitive, mergeFieldStyle, mergeInputStyle } from './shared'

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

export function DomainPicker({ values, onChange }: { values: string[]; onChange: (v: string[]) => void }) {
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
