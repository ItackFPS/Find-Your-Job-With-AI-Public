import { useCallback, useState } from 'react'
import { applyTheme, readThemePreference, writeThemePreference, type ThemePreference } from './theme'

/** The current theme preference and a setter that stores and applies it. */
export function useTheme(): [ThemePreference, (preference: ThemePreference) => void] {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference)
  const update = useCallback((next: ThemePreference) => {
    writeThemePreference(next)
    applyTheme(next)
    setPreference(next)
  }, [])
  return [preference, update]
}
