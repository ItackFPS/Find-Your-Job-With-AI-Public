import i18n, { PSEUDO_LOCALE } from '../i18n'

// Dates follow the UI language, not the browser's. The pseudo-locale formats like British
// English (24-hour clock), so it adds no untranslatable "AM"/"PM".
function dateLocale(): string {
  return i18n.language === PSEUDO_LOCALE ? 'en-GB' : i18n.language
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(dateLocale())
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(dateLocale())
}

/** Whole days between now and `iso`, or null when there's no date. */
export function daysAgo(iso: string | null | undefined): number | null {
  if (!iso) return null
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
}
