// Contrast of the dark palette, read from the stylesheet's own tokens (spec 10.4, item 2).
//
//   node scripts/contrast.mjs            # prints the report as markdown
//   node scripts/contrast.mjs --json     # the same as data
//
// The tokens are read, not copied: the last `:root[data-theme="dark"] { ... }` blocks of
// src/styles/page.css are merged in order (later wins, as in the cascade), so a token changed
// there changes this report and the gate over it (scripts/gates/contrast.test.mjs).
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const CSS = join(HERE, '..', 'src', 'styles', 'page.css')

/** The dark theme's tokens: every `--name: value` of the `:root[data-theme="dark"]` blocks, merged. */
export function darkTokens(css = readFileSync(CSS, 'utf8')) {
  const tokens = {}
  for (const block of css.matchAll(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/g)) {
    for (const decl of block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) tokens[decl[1]] = decl[2].trim()
  }
  return tokens
}

const hex = (value) => {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value)
  if (!m) return null
  const h = m[1].length === 3 ? [...m[1]].map((c) => c + c).join('') : m[1]
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16))
}

const channel = (v) => {
  const s = v / 255
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
}
const luminance = ([r, g, b]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

/** WCAG 2.x contrast ratio of two #rgb / #rrggbb colours, or null when either is not one. */
export function ratio(fg, bg) {
  const a = hex(fg)
  const b = hex(bg)
  if (!a || !b) return null
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/* What the spec names: text and captions on every surface they sit on, the status colours on the
   page and its panels, and the ink on the amber button. `min` is the bar: 4.5 for text, 3 for the
   large or graphic uses. `faint` is "only auxiliary text, and only if it passes" (10.2), so it is
   reported against 3 -- the bar for large text and glyphs -- and its use stays the page's rule. */
export const SURFACES = ['--page', '--surface', '--raised', '--card', '--card-2']
export const PAIRS = [
  ...['--text', '--muted'].flatMap((fg) => SURFACES.map((bg) => ({ fg, bg, min: 4.5 }))),
  ...['--amber', '--moss', '--clay', '--sky', '--link'].flatMap((fg) =>
    ['--page', '--surface', '--raised'].map((bg) => ({ fg, bg, min: 4.5 }))),
  { fg: '--amber-ink', bg: '--amber', min: 4.5 },
  ...['--page', '--surface', '--raised'].map((bg) => ({ fg: '--faint', bg, min: 3 })),
]

export function report(tokens = darkTokens()) {
  return PAIRS.map((p) => {
    const fg = tokens[p.fg]
    const bg = tokens[p.bg]
    const r = fg && bg ? ratio(fg, bg) : null
    return { ...p, fgValue: fg ?? null, bgValue: bg ?? null, ratio: r, pass: r !== null && r >= p.min }
  })
}

if (process.argv[1]?.endsWith('contrast.mjs')) {
  const rows = report()
  if (process.argv.includes('--json')) {
    console.log(JSON.stringify(rows, null, 2))
  } else {
    console.log('| Текст | Фон | Контраст | Порог | Итог |\n|---|---|---|---|---|')
    for (const r of rows) {
      const shown = r.ratio === null ? 'нет значения' : r.ratio.toFixed(2)
      console.log(`| \`${r.fg}\` ${r.fgValue ?? ''} | \`${r.bg}\` ${r.bgValue ?? ''} | ${shown} | ${r.min} | ${r.pass ? 'да' : 'НЕТ'} |`)
    }
  }
}
