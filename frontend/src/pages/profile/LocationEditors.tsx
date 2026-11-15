import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getAllDepartments, searchPlaces, type FrenchDepartment, type LocationPreference, type HomePlace, type PlaceSuggestion } from '../../api/client'
import { useTranslation } from 'react-i18next'
import i18n from '../../i18n'
import { chipRemoveStyle, dropdownStyle, iconButtonStyle, inputStyle, mergeFieldStyle, mergeInputStyle } from './shared'

// Labels: profile.location.status.<status>
const LOCATION_STATUSES: LocationPreference['status'][] = ['preferred', 'acceptable', 'excluded']

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

export function LocationPreferencesEditor({ values, onChange }: {
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
                aria-label={t('profile.location.statusLabel')}
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

export function HomePicker({ value, onChange }: { value: HomePlace | null; onChange: (v: HomePlace | null) => void }) {
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
