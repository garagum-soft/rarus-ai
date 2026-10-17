// While the program runs the page asks nothing of a CDN or a font host (spec 10.4, item 5).
//
// The page is one file with its stylesheet and script inline (build.py), and the fonts are the
// system's. So the way this breaks is a stylesheet that `@import`s or `url()`s an http address, a
// `<link>` or `<script>` that loads one, or a font host named in code -- each of which would make
// the first paint wait on the network and leak the reader's address to a third party. Links the
// reader clicks to open in their browser are not loads and are not read here.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { relPath, root } from './paths.mjs'

const SRC = join(root(new URL('../..', import.meta.url)), 'src')

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) yield* files(path)
    else yield path
  }
}

const LOADS = [
  [/@import\s+(?:url\(\s*)?['"]?https?:/i, 'a stylesheet @import of an http address'],
  [/url\(\s*['"]?(?:https?:)?\/\//i, 'a url() of an http address'],
  [/<link\b[^>]*\bhref\s*=\s*['"](?:https?:)?\/\//i, 'a <link> to an http address'],
  [/<script\b[^>]*\bsrc\s*=\s*['"](?:https?:)?\/\//i, 'a <script src> of an http address'],
  [/fonts\.(?:googleapis|gstatic)\.com|use\.typekit\.net|cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com/i, 'a CDN or font host'],
]

describe('what the page loads', () => {
  const sources = [...files(SRC)].filter((p) => /\.(css|html|ts|tsx|mjs)$/.test(p) && !/\.test\./.test(p))

  it('reads the tree it is meant to', () => {
    expect(sources.length).toBeGreaterThan(100)
    expect(sources.some((p) => p.endsWith('page.css'))).toBe(true)
    expect(sources.some((p) => p.endsWith('page.html'))).toBe(true)
  })

  it('names no http address to load from', () => {
    const found = []
    for (const path of sources) {
      const text = readFileSync(path, 'utf8')
      for (const [pattern, what] of LOADS) {
        if (pattern.test(text)) found.push(`${relPath(SRC, path)}: ${what}`)
      }
    }
    expect(found).toEqual([])
  })
})
