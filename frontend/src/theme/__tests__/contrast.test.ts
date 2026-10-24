import { describe, it, expect } from 'vitest'
import tokensCss from '../../styles/tokens.css?raw'
import { contrastRatio, mix, parseColour, type Rgb } from '../contrast'
import { themeTokens, type ThemeName } from '../tokens'

// WCAG 2.2 AA: 4.5:1 for body text (1.4.3), 3:1 for UI components, focus indicators and
// large text (1.4.11). Every pair below is one the UI actually draws.
const TEXT = 4.5
const UI = 3

const SURFACES = ['bg', 'surface', 'surface-elevated'] as const
const STATUS = ['success', 'warning', 'danger', 'info', 'tier-strong', 'accent'] as const
// Tinted backgrounds used by Chip, StatusBadge and the selected pills (13 % of the colour).
const TINT = 0.13

type Pair = { fg: string; bg: string; fgRgb: Rgb; bgRgb: Rgb; min: number }

function pairsFor(theme: ThemeName): Pair[] {
  const t = themeTokens(tokensCss, theme)
  const c = (name: string) => parseColour(t[`--color-${name}`])
  const pairs: Pair[] = []
  const add = (fg: string, bg: string, min: number, fgRgb = c(fg), bgRgb = c(bg)) =>
    pairs.push({ fg, bg, fgRgb, bgRgb, min })

  for (const s of SURFACES) {
    add('text', s, TEXT)
    add('muted', s, TEXT)
    add('focus', s, UI)
  }
  for (const s of ['bg', 'surface'] as const) {
    add('link', s, TEXT)
    add('border-strong', s, UI)
    for (const status of STATUS) {
      add(status, s, TEXT)
      add(status, `${status} tint on ${s}`, TEXT, c(status), mix(c(status), c(s), TINT))
    }
  }
  add('on-accent', 'accent', TEXT)
  add('accent', 'accent-dim', UI) // Switch knob on its checked track
  return pairs
}

describe.each(['light', 'dark'] as const)('%s theme tokens meet WCAG 2.2 AA', theme => {
  it.each(pairsFor(theme).map(p => [`${p.fg} on ${p.bg}`, p] as const))('%s', (_, p) => {
    const ratio = contrastRatio(p.fgRgb, p.bgRgb)
    expect(ratio, `${p.fg} on ${p.bg} = ${ratio.toFixed(2)}:1, needs ${p.min}:1`).toBeGreaterThanOrEqual(p.min)
  })
})

describe('tokens.css parsing', () => {
  it('defines the same colour tokens in both themes', () => {
    const light = Object.keys(themeTokens(tokensCss, 'light')).filter(k => k.startsWith('--color-')).sort()
    const dark = Object.keys(themeTokens(tokensCss, 'dark')).filter(k => k.startsWith('--color-')).sort()
    expect(dark).toEqual(light)
    expect(light).toEqual(expect.arrayContaining(
      ['bg', 'surface', 'text', 'muted', 'border', 'accent', 'success', 'warning', 'danger', 'info', 'focus'].map(n => `--color-${n}`),
    ))
  })
})
