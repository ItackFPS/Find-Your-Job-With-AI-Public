// UI translations (F7.1): i18next with ICU message format. English is the source and the
// fallback; French is complete. The pseudo-locale en-XA exists in development and tests only.
import i18n from 'i18next'
import ICU from 'i18next-icu'
import { initReactI18next } from 'react-i18next'
import en from '../locales/en/translation.json'
import fr from '../locales/fr/translation.json'
import { pseudoLocalizeTree } from './pseudo'

export const LANGUAGE_STORAGE_KEY = 'fyj.lang'
export const PSEUDO_LOCALE = 'en-XA'
export const LANGUAGES = ['en', 'fr'] as const
export type Language = (typeof LANGUAGES)[number] | typeof PSEUDO_LOCALE

export const PSEUDO_ENABLED = import.meta.env.DEV || import.meta.env.MODE === 'test'

const resources = {
  en: { translation: en },
  fr: { translation: fr },
  ...(PSEUDO_ENABLED ? { [PSEUDO_LOCALE]: { translation: pseudoLocalizeTree(en) } } : {}),
}

export function availableLanguages(): Language[] {
  return PSEUDO_ENABLED ? [...LANGUAGES, PSEUDO_LOCALE] : [...LANGUAGES]
}

function isAvailable(lng: string | null): lng is Language {
  return lng !== null && (availableLanguages() as string[]).includes(lng)
}

/** The stored choice, else the browser language when we have it, else English. */
export function detectLanguage(): Language {
  try {
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY)
    if (isAvailable(stored)) return stored
  } catch {
    // storage unavailable: fall through to the browser language
  }
  const browser = (typeof navigator !== 'undefined' ? navigator.language : 'en').toLowerCase()
  return browser.startsWith('fr') ? 'fr' : 'en'
}

export function setLanguage(lng: Language): void {
  try {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, lng)
  } catch {
    // private mode: the choice still applies for this session
  }
  void i18n.changeLanguage(lng)
}

i18n.on('languageChanged', lng => {
  if (typeof document !== 'undefined') document.documentElement.lang = lng
})

void i18n
  .use(ICU)
  .use(initReactI18next)
  .init({
    resources,
    lng: detectLanguage(),
    fallbackLng: 'en',
    supportedLngs: availableLanguages(),
    interpolation: { escapeValue: false },
    returnNull: false,
    // Resources are bundled: initialise synchronously so the first render is translated.
    initAsync: false,
  })

export default i18n
