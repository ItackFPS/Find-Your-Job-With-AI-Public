// Read the colour tokens of each theme out of src/styles/tokens.css text, resolving
// var() references. Used by the contrast test, so the test checks the shipped CSS rather
// than a copy of it.

export type ThemeName = 'light' | 'dark'

function declarations(block: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim()
  return out
}

function blockFor(css: string, selectorTest: (selector: string) => boolean): string {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '')
  for (const m of clean.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (selectorTest(m[1].trim())) return m[2]
  }
  throw new Error('Theme block not found in tokens.css')
}

/** `{ '--color-bg': '#f5f6fa', ... }` for one theme, with var() resolved. */
export function themeTokens(css: string, theme: ThemeName): Record<string, string> {
  const block =
    theme === 'light'
      ? blockFor(css, s => s.includes(":root") && s.includes("[data-theme='light']"))
      : blockFor(css, s => s === "[data-theme='dark']")
  const raw = declarations(block)
  const resolve = (value: string, depth = 0): string =>
    depth > 10 ? value : value.replace(/var\((--[\w-]+)\)/g, (_, name: string) => resolve(raw[name] ?? '', depth + 1))
  return Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, resolve(v)]))
}
