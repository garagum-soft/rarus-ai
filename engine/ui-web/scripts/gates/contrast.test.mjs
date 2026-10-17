// The dark palette clears the contrast bars the design names (spec 10.4, item 2): body and
// caption text at 4.5:1 on every surface it sits on, the status colours and links at 4.5:1 on the
// page and its panels, the ink on the amber button at 4.5:1, and the faint grey -- auxiliary text
// only -- at the 3:1 that large text and glyphs need.
//
// Read from the stylesheet's own tokens (scripts/contrast.mjs), so a colour changed there is
// checked here and the report (docs/reports/contrast.md) is `node scripts/contrast.mjs`.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { darkTokens, PAIRS, ratio, report } from '../contrast.mjs'
import { root } from './paths.mjs'

const CSS = join(root(new URL('../..', import.meta.url)), 'src', 'styles', 'page.css')

describe('the dark palette', () => {
  const tokens = darkTokens()

  it('has a colour for every token the checks name', () => {
    const missing = PAIRS.flatMap((p) => [p.fg, p.bg]).filter((name) => !tokens[name])
    expect([...new Set(missing)]).toEqual([])
  })

  it('clears every bar', () => {
    const failing = report(tokens)
      .filter((row) => !row.pass)
      .map((row) => `${row.fg} ${row.fgValue} on ${row.bg} ${row.bgValue}: ${row.ratio?.toFixed(2)} < ${row.min}`)
    expect(failing, 'lift or darken the token in the Rarus block at the end of src/styles/page.css').toEqual([])
  })

  it('measures contrast the way WCAG does', () => {
    expect(ratio('#000000', '#ffffff')).toBeCloseTo(21, 5)
    expect(ratio('#777777', '#777777')).toBeCloseTo(1, 5)
    expect(ratio('#fff', '#000')).toBeCloseTo(21, 5)
    expect(ratio('not a colour', '#000')).toBeNull()
  })

  it('says the same under the system preference as under the explicit toggle', () => {
    /* The Rarus block is written twice -- once for `data-theme="dark"`, once for a system that
       is dark and a page that picked nothing -- and a token changed in one place only would make
       the two disagree. */
    const css = readFileSync(CSS, 'utf8')
    const explicit = [...css.matchAll(/:root\[data-theme="dark"\]\s*\{([^}]*--amber-ink[^}]*)\}/g)].pop()
    const system = [...css.matchAll(/@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{([^}]*--amber-ink[^}]*)\}/g)].pop()
    expect(explicit, 'the Rarus dark block').toBeTruthy()
    expect(system, 'the Rarus system-dark block').toBeTruthy()
    const decls = (body) => Object.fromEntries([...body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]))
    expect(decls(system[1])).toEqual(decls(explicit[1]))
  })
})
