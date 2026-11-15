import { describe, it, expect } from 'vitest'
import fr from '../../locales/fr/translation.json'

// Phase B B10, P-014: the French interface speaks French, not developer English. Placeholders
// ({name}, {count, plural, …}) and tags (<b>, <run>) are code, not text, and are left out. The
// experiment lab and the design-system page are developer tools and are not checked.
const DEVELOPER_TERMS = /\b(pipeline|scans?|shadow|token|debug|batch|dashboard|feedback|matching|scraping|backend|frontend|endpoint|payload|fallback|boost|embeddings?|LLM|ranking|scheduler|staging|checkpoint|canary|breaker|preflight)\b/i
const DEVELOPER_PAGES = ['lab.', 'design.']

function flatten(node: unknown, prefix = ''): [string, string][] {
  if (typeof node === 'string') return [[prefix, node]]
  if (node && typeof node === 'object') {
    return Object.entries(node as Record<string, unknown>).flatMap(([k, v]) => flatten(v, prefix ? `${prefix}.${k}` : k))
  }
  return []
}

function visibleText(message: string): string {
  let text = message.replace(/<\/?[a-zA-Z][^>]*>/g, ' ')
  // drop ICU arguments, innermost first, keeping the words of plural/select branches
  for (let i = 0; i < 5; i++) text = text.replace(/\{[a-zA-Z_][\w]*(,\s*\w+)?\}/g, ' ')
  return text.replace(/\{[a-zA-Z_][\w]*,\s*(plural|select|selectordinal),/g, ' ')
}

describe('French wording (P-014)', () => {
  it('no French string shows a developer term', () => {
    const offending = flatten(fr)
      .filter(([key]) => !DEVELOPER_PAGES.some(p => key.startsWith(p)))
      .filter(([, value]) => DEVELOPER_TERMS.test(visibleText(value)))
      .map(([key, value]) => `${key} = ${value}`)
    expect(offending).toEqual([])
  })

  it('placeholders do not count as words', () => {
    expect(visibleText('Sur {runs} collectes, {count, plural, one {# offre} other {# offres}}')).not.toMatch(DEVELOPER_TERMS)
    expect(DEVELOPER_TERMS.test(visibleText('Lancer le <b>pipeline</b>'))).toBe(true)
  })
})
