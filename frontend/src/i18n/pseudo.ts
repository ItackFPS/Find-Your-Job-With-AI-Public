// Pseudo-locale (en-XA): every letter accented, vowels doubled (about 35 % longer, like
// German), the whole message wrapped in [!! … !!]. Any plain ASCII word left on screen under
// this locale was not translated; any clipped bracket shows a layout that cannot grow.
// ICU syntax ({name}, {n, plural, one {…} other {…}}, #) and <0>…</0> tags are kept intact.

const ACCENTS: Record<string, string> = {
  a: 'å', b: 'ƀ', c: 'ç', d: 'ð', e: 'é', f: 'ƒ', g: 'ĝ', h: 'ĥ', i: 'î', j: 'ĵ', k: 'ķ', l: 'ļ', m: 'ɱ',
  n: 'ñ', o: 'ö', p: 'þ', q: 'ǫ', r: 'ŕ', s: 'š', t: 'ţ', u: 'û', v: 'ṽ', w: 'ŵ', x: 'ẋ', y: 'ý', z: 'ž',
  A: 'Å', B: 'Ɓ', C: 'Ç', D: 'Ð', E: 'É', F: 'Ƒ', G: 'Ĝ', H: 'Ĥ', I: 'Î', J: 'Ĵ', K: 'Ķ', L: 'Ļ', M: 'Ṁ',
  N: 'Ñ', O: 'Ö', P: 'Þ', Q: 'Ǫ', R: 'Ŕ', S: 'Š', T: 'Ţ', U: 'Û', V: 'Ṽ', W: 'Ŵ', X: 'Ẋ', Y: 'Ý', Z: 'Ž',
}
const VOWELS = new Set(['a', 'e', 'i', 'o', 'u', 'A', 'E', 'I', 'O', 'U'])

function accentText(text: string): string {
  let out = ''
  for (const c of text) {
    const accented = ACCENTS[c] ?? c
    out += VOWELS.has(c) ? accented + accented.toLowerCase() : accented
  }
  return out
}

/** Index of the brace that closes the one opened at `start`. */
function closingBrace(s: string, start: number): number {
  let depth = 0
  for (let i = start; i < s.length; i++) {
    if (s[i] === '{') depth++
    else if (s[i] === '}' && --depth === 0) return i
  }
  return s.length - 1
}

/** Split on the first two top-level commas: "name, plural, rest". */
function argumentParts(inner: string): string[] {
  const parts: string[] = []
  let depth = 0
  let last = 0
  for (let i = 0; i < inner.length && parts.length < 2; i++) {
    if (inner[i] === '{') depth++
    else if (inner[i] === '}') depth--
    else if (inner[i] === ',' && depth === 0) {
      parts.push(inner.slice(last, i))
      last = i + 1
    }
  }
  parts.push(inner.slice(last))
  return parts
}

function transformArgument(inner: string): string {
  const parts = argumentParts(inner)
  if (parts.length < 3 || !['plural', 'select', 'selectordinal'].includes(parts[1].trim())) return `{${inner}}`
  const options = parts[2]
  let out = ''
  for (let i = 0; i < options.length; i++) {
    if (options[i] === '{') {
      const end = closingBrace(options, i)
      out += `{${transformMessage(options.slice(i + 1, end))}}`
      i = end
    } else {
      out += options[i]
    }
  }
  return `{${parts[0]},${parts[1]},${out}}`
}

function transformMessage(message: string): string {
  let out = ''
  for (let i = 0; i < message.length; i++) {
    const c = message[i]
    if (c === '{') {
      const end = closingBrace(message, i)
      out += transformArgument(message.slice(i + 1, end))
      i = end
    } else if (c === '<' && /^<\/?\w+\s*\/?>/.test(message.slice(i))) {
      const end = message.indexOf('>', i)
      out += message.slice(i, end + 1)
      i = end
    } else {
      out += accentText(c)
    }
  }
  return out
}

export function pseudoLocalize(message: string): string {
  return `[!! ${transformMessage(message)} !!]`
}

export type LocaleTree = { [key: string]: string | LocaleTree }

export function pseudoLocalizeTree(tree: LocaleTree): LocaleTree {
  return Object.fromEntries(
    Object.entries(tree).map(([k, v]) => [k, typeof v === 'string' ? pseudoLocalize(v) : pseudoLocalizeTree(v)]),
  )
}
