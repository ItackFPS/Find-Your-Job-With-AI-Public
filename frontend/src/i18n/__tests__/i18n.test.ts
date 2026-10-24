import { describe, it, expect, afterEach, vi } from 'vitest'
import { IntlMessageFormat } from 'intl-messageformat'
import i18n, { detectLanguage, LANGUAGE_STORAGE_KEY, setLanguage } from '..'
import { pseudoLocalize } from '../pseudo'

describe('pseudoLocalize', () => {
  it('accents and lengthens text, wrapped in markers', () => {
    const out = pseudoLocalize('Save profile')
    expect(out.startsWith('[!! ') && out.endsWith(' !!]')).toBe(true)
    expect(out).not.toMatch(/[A-Za-z]{2,}/)
    expect(out.length).toBeGreaterThan('Save profile'.length * 1.3)
  })

  it('keeps ICU placeholders, plural keywords, # and tags working', () => {
    const msg = pseudoLocalize('{count, plural, one {# job} other {# jobs}} in <b>{place}</b> <cmd/>')
    const formatted = new IntlMessageFormat(msg, 'en', undefined, { ignoreTag: true }).format({ count: 3, place: 'Toulon' })
    expect(formatted).toContain('3 ĵööƀš')
    expect(formatted).toContain('<b>Toulon</b>')
    expect(formatted).toContain('<cmd/>')
  })
})

describe('language selection', () => {
  afterEach(async () => {
    window.localStorage.clear()
    vi.restoreAllMocks()
    await i18n.changeLanguage('en')
  })

  it('stores the chosen language and switches the UI to it', () => {
    setLanguage('fr')
    expect(window.localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe('fr')
    expect(i18n.language).toBe('fr')
    expect(document.documentElement.lang).toBe('fr')
    expect(i18n.t('jobDetail.personalization.feedbackCount', { count: 2 })).toBe(' (2 retours)')
  })

  it('prefers the stored choice, then a French browser, then English', () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'fr')
    expect(detectLanguage()).toBe('fr')
    window.localStorage.removeItem(LANGUAGE_STORAGE_KEY)
    vi.spyOn(navigator, 'language', 'get').mockReturnValue('fr-FR')
    expect(detectLanguage()).toBe('fr')
    vi.spyOn(navigator, 'language', 'get').mockReturnValue('de-DE')
    expect(detectLanguage()).toBe('en')
  })

  it('ignores an unknown stored language and survives storage that throws', () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'xx')
    vi.spyOn(navigator, 'language', 'get').mockReturnValue('en-US')
    expect(detectLanguage()).toBe('en')
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    expect(detectLanguage()).toBe('en')
    expect(() => setLanguage('fr')).not.toThrow()
  })
})
