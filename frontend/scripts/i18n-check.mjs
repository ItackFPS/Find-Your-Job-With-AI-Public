// Translation catalogue check (F7.1), run by `npm run i18n:check` and the frontend CI job.
// Fails when, compared with English (the source):
//   - a locale is missing a key, or has a key English does not have;
//   - a message is not valid ICU, or uses other {placeholders} than the English one;
//   - an English key is used nowhere in src/ (a dead string).
// A key counts as used when it appears as a string literal in src/, or starts with the
// static prefix of a template literal such as t(`status.${code}`).
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { IntlMessageFormat } from 'intl-messageformat'

const root = fileURLToPath(new URL('..', import.meta.url))
const localesDir = join(root, 'src', 'locales')
const srcDir = join(root, 'src')

function flatten(tree, prefix = '', out = {}) {
  for (const [k, v] of Object.entries(tree)) {
    if (typeof v === 'string') out[prefix + k] = v
    else flatten(v, `${prefix}${k}.`, out)
  }
  return out
}

function argumentNames(elements, out = new Set()) {
  for (const el of elements) {
    // types: 1 argument, 2 number, 3 date, 4 time, 5 select, 6 plural, 8 tag
    if ([1, 2, 3, 4, 5, 6].includes(el.type)) out.add(el.value)
    if (el.options) for (const opt of Object.values(el.options)) argumentNames(opt.value, out)
    if (el.children) argumentNames(el.children, out)
  }
  return out
}

function parse(message, lng) {
  const ast = new IntlMessageFormat(message, lng, undefined, { ignoreTag: true }).getAst()
  return [...argumentNames(ast)].sort().join(',')
}

const problems = []
const locales = readdirSync(localesDir).filter(d => statSync(join(localesDir, d)).isDirectory())
const catalogue = Object.fromEntries(
  locales.map(lng => [lng, flatten(JSON.parse(readFileSync(join(localesDir, lng, 'translation.json'), 'utf8')))]),
)
const en = catalogue.en
if (!en) throw new Error('src/locales/en/translation.json is missing')

const enArgs = {}
for (const [key, msg] of Object.entries(en)) {
  try { enArgs[key] = parse(msg, 'en') } catch (e) { problems.push(`en: invalid ICU in ${key}: ${e.message}`) }
}

for (const lng of locales.filter(l => l !== 'en')) {
  const messages = catalogue[lng]
  const missing = Object.keys(en).filter(k => !(k in messages))
  const extra = Object.keys(messages).filter(k => !(k in en))
  missing.forEach(k => problems.push(`${lng}: missing ${k}`))
  extra.forEach(k => problems.push(`${lng}: not in en ${k}`))
  for (const [key, msg] of Object.entries(messages)) {
    if (!(key in en)) continue
    try {
      const args = parse(msg, lng)
      if (enArgs[key] !== undefined && args !== enArgs[key]) problems.push(`${lng}: ${key} uses {${args}} but en uses {${enArgs[key]}}`)
    } catch (e) {
      problems.push(`${lng}: invalid ICU in ${key}: ${e.message}`)
    }
  }
  console.log(`${lng}: ${Object.keys(messages).length} keys, ${missing.length} missing, ${extra.length} not in en`)
}

// Unused keys
function sourceFiles(dir) {
  return readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === 'locales' || name === '__tests__' ? [] : sourceFiles(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : []
  })
}
const literals = new Set()
const prefixes = new Set()
for (const file of sourceFiles(srcDir)) {
  const text = readFileSync(file, 'utf8')
  for (const m of text.matchAll(/''|""|'([^'\n\\]+)'|"([^"\n\\]+)"/g)) if (m[1] ?? m[2]) literals.add(m[1] ?? m[2])
  for (const m of text.matchAll(/`([^`$]*)\$\{/g)) if (m[1].includes('.')) prefixes.add(m[1])
}
const unused = Object.keys(en).filter(k => !literals.has(k) && ![...prefixes].some(p => k.startsWith(p)))
unused.forEach(k => problems.push(`en: unused ${k}`))
console.log(`en: ${Object.keys(en).length} keys, ${unused.length} unused (scanned ${relative(root, srcDir)})`)

if (problems.length) {
  console.error(`\n${problems.length} problem(s):\n  ${problems.join('\n  ')}`)
  process.exit(1)
}
console.log('i18n check passed')
