import { useState, useEffect, useRef, useCallback } from 'react'
import { searchEscoSkills, type EscoEntry, type EscoSuggestion } from '../../api/client'
import { useTranslation } from 'react-i18next'
import { TAG_TONE_STYLES, chipRemoveStyle, dropdownStyle, includesCaseInsensitive, mergeFieldStyle, mergeInputStyle } from './shared'
import type { TagTone } from './shared'

export function TagList({ values, onChange, tone = 'default', placeholder }: {
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

export interface SkillsState {
  skills: string[]
  escoEntries: EscoEntry[]
}

interface AutocompleteTagListProps {
  state: SkillsState
  onChange: (s: SkillsState) => void
  lang?: 'en' | 'fr'
}

export function AutocompleteTagList({ state, onChange, lang = 'en' }: AutocompleteTagListProps) {
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
