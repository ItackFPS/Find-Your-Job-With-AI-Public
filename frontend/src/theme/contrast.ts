// WCAG 2.x contrast maths, shared by the contrast test and the /design page.

export type Rgb = [number, number, number]

export function parseHex(hex: string): Rgb {
  const h = hex.trim().replace('#', '')
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h.slice(0, 6)
  if (!/^[0-9a-fA-F]{6}$/.test(full)) throw new Error(`Not a hex colour: ${hex}`)
  return [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16)) as Rgb
}

/** Parse "#rrggbb", "#rgb" or "rgb(r, g, b)" / "rgb(r g b)". */
export function parseColour(value: string): Rgb {
  const v = value.trim()
  if (v.startsWith('#')) return parseHex(v)
  const m = v.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/)
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])]
  throw new Error(`Unsupported colour: ${value}`)
}

function channel(c: number): number {
  const s = c / 255
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
}

export function relativeLuminance([r, g, b]: Rgb): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

export function contrastRatio(a: Rgb, b: Rgb): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/** `color-mix(in srgb, fg p%, bg)`: the colour of `fg` at `weight` (0-1) over `bg`. */
export function mix(fg: Rgb, bg: Rgb, weight: number): Rgb {
  return fg.map((c, i) => Math.round(c * weight + bg[i] * (1 - weight))) as Rgb
}
