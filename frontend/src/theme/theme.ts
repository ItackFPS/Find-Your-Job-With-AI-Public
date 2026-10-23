// Light/dark theme (F10.1). Light is the default; "system" follows prefers-color-scheme and
// is what a new user gets. The user's explicit choice is kept in localStorage. The resolved
// theme is written to <html data-theme="light|dark">, which selects the tokens in
// src/styles/tokens.css.

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'fyj.theme'
const PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark']
const DARK_QUERY = '(prefers-color-scheme: dark)'

/** The stored choice, or "system" when none is stored or storage is unavailable. */
export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return PREFERENCES.includes(stored as ThemePreference) ? (stored as ThemePreference) : 'system'
  } catch {
    return 'system'
  }
}

export function writeThemePreference(preference: ThemePreference): void {
  try {
    if (preference === 'system') window.localStorage.removeItem(THEME_STORAGE_KEY)
    else window.localStorage.setItem(THEME_STORAGE_KEY, preference)
  } catch {
    // Private mode or storage disabled: the choice still applies for this session.
  }
}

function systemPrefersDark(): boolean {
  try {
    return typeof window.matchMedia === 'function' && window.matchMedia(DARK_QUERY).matches
  } catch {
    return false
  }
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === 'system') return systemPrefersDark() ? 'dark' : 'light'
  return preference
}

export function applyTheme(preference: ThemePreference, root: HTMLElement = document.documentElement): ResolvedTheme {
  const resolved = resolveTheme(preference)
  root.dataset.theme = resolved
  return resolved
}

/**
 * Apply the stored preference now and keep following the system while it is "system".
 * Returns a function that stops listening (used by tests).
 */
export function initTheme(): () => void {
  applyTheme(readThemePreference())
  if (typeof window.matchMedia !== 'function') return () => {}
  const media = window.matchMedia(DARK_QUERY)
  const onChange = () => {
    if (readThemePreference() === 'system') applyTheme('system')
  }
  media.addEventListener?.('change', onChange)
  return () => media.removeEventListener?.('change', onChange)
}
