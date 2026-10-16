/**
 * Text bound for the terminal, reduced to the escape sequences the renderer
 * itself understands.
 *
 * The tokenizer in front of the screen buffer (@alcalzone/ansi-tokenize) reads
 * any ESC with a digit somewhere in the next 19 characters as a style code, and
 * the style pool writes style codes to the terminal verbatim. Text the renderer
 * did not write -- a tool's output, a file the agent read, a model's answer, a
 * fetched page title -- could therefore set the clipboard (OSC 52), retitle the
 * window, leave the alternate screen or switch the mouse off. What stays is SGR
 * colour and style and an OSC 8 hyperlink whose parameters and URI are
 * printable ASCII; every other sequence is dropped whole, the way the screen
 * writer already skips the ones the tokenizer did recognise as unknown. C1
 * controls go too, since some terminals read U+009B as CSI.
 */

const ESC = '\x1b'
const BEL = '\x07'

const SGR = /\x1b\[[0-9;:]*m/y
const OSC8 = /\x1b\]8;[\x20-\x3a\x3c-\x7e]*;[\x21-\x7e]*(?:\x07|\x1b\\)/y
const NEEDS_WORK = /[\x1b\x80-\x9f]/

/** Where the escape sequence starting at `start` ends, exclusive. */
function sequenceEnd(text: string, start: number): number {
  const next = text[start + 1]

  if (next === undefined) {
    return start + 1
  }

  if (next === '(' || next === ')' || next === '*' || next === '+') {
    return Math.min(start + 3, text.length)
  }

  if (next === '[') {
    let i = start + 2

    while (i < text.length) {
      const code = text.charCodeAt(i)

      i++

      if (code >= 0x40 && code <= 0x7e) {
        break
      }
    }

    return i
  }

  if (next === ']' || next === 'P' || next === '_' || next === '^' || next === 'X') {
    let i = start + 2

    while (i < text.length) {
      if (text[i] === BEL) {
        return i + 1
      }

      if (text[i] === ESC && text[i + 1] === '\\') {
        return i + 2
      }

      i++
    }

    return i
  }

  const code = next.charCodeAt(0)

  return code >= 0x30 && code <= 0x7e ? start + 2 : start + 1
}

function matchAt(pattern: RegExp, text: string, index: number): number {
  pattern.lastIndex = index

  return pattern.test(text) ? pattern.lastIndex : -1
}

export function sanitizeTerminalText(text: string): string {
  if (!NEEDS_WORK.test(text)) {
    return text
  }

  let out = ''
  let i = 0

  while (i < text.length) {
    const code = text.charCodeAt(i)

    if (code >= 0x80 && code <= 0x9f) {
      i++

      continue
    }

    if (text[i] !== ESC) {
      out += text[i]
      i++

      continue
    }

    const kept = Math.max(matchAt(SGR, text, i), matchAt(OSC8, text, i))

    if (kept > i) {
      out += text.slice(i, kept)
      i = kept

      continue
    }

    i = sequenceEnd(text, i)
  }

  return out
}

/** Whether a string may sit inside an OSC 8 sequence: printable ASCII only. */
export function isSafeLinkText(text: string, allowSpace = false): boolean {
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i)

    if (code > 0x7e || code < (allowSpace ? 0x20 : 0x21)) {
      return false
    }
  }

  return true
}
