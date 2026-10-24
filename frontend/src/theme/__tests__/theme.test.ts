import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { applyTheme, initTheme, readThemePreference, resolveTheme, THEME_STORAGE_KEY, writeThemePreference } from '../theme'

function mockSystemDark(dark: boolean) {
  window.matchMedia = vi.fn().mockReturnValue({
    matches: dark, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }) as unknown as typeof window.matchMedia
}

describe('theme', () => {
  const originalMatchMedia = window.matchMedia
  beforeEach(() => window.localStorage.clear())
  afterEach(() => {
    window.matchMedia = originalMatchMedia
    vi.restoreAllMocks()
    delete document.documentElement.dataset.theme
  })

  it('defaults to following the system, which is light when no dark preference is reported', () => {
    mockSystemDark(false)
    expect(readThemePreference()).toBe('system')
    expect(resolveTheme('system')).toBe('light')
  })

  it('follows a dark system preference', () => {
    mockSystemDark(true)
    initTheme()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('lets a stored user choice override the system', () => {
    mockSystemDark(true)
    writeThemePreference('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    initTheme()
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('forgets the stored choice when the user goes back to "system"', () => {
    writeThemePreference('dark')
    writeThemePreference('system')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
  })

  it('ignores an invalid stored value', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'neon')
    expect(readThemePreference()).toBe('system')
  })

  it('survives storage that throws (private mode)', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    expect(readThemePreference()).toBe('system')
    expect(() => writeThemePreference('dark')).not.toThrow()
    expect(applyTheme('dark')).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })
})
