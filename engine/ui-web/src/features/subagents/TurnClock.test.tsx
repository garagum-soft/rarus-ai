// @vitest-environment happy-dom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { TurnClock } from './TurnClock'

import type { InstanceRow } from './types'

function wire(): void {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
}

const row = (over: Partial<InstanceRow> = {}): InstanceRow =>
  ({ sessionKey: 's1', agent: 'hermes', handle: 'one', kind: 'cli', ...over }) as InstanceRow

const shown = (): string | null => document.querySelector('.pane-turnms')?.textContent ?? null

afterEach(() => {
  cleanup()
  resetTranslator()
  vi.useRealTimers()
})

describe('the turn clock on an instance pane', () => {
  it('draws nothing for an instance answering nothing', () => {
    /* Absent is the signal, so an instance that finished between two polls stops
       counting rather than freezing on its last number. */
    wire()
    render(<TurnClock row={row()} />)
    expect(shown()).toBeNull()
  })

  it('counts from the turn start the server published', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_090_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    /* 90s, in the spelling `lib/duration.ts` gives every other elapsed number
       on the page. */
    expect(shown()).toBe('1m30s')
  })

  it('will not fall back to the row stamp when no turn is running', () => {
    /* `updatedAtMs` sits on the same row and is the tempting field: it is always
       there and it looks like a time. Every registry write stamps it -- a
       binding commit, a graph-origin write -- so a clock counting from it dates
       the row rather than the turn, and would run on an instance that is
       answering nothing at all. The row here carries only that stamp. */
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_090_000)
    render(<TurnClock row={row({ updatedAtMs: 1_700_000_000_000 })} />)
    expect(shown()).toBeNull()
  })

  it('holds back the first second rather than opening on zero', () => {
    /* A clock that opens on "0.0s" reads as broken rather than as new, which is
       the rule the transcript's own card already follows. */
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_400)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    expect(shown()).toBeNull()
  })

  it('names what the number measures, for a reader who cannot see the pane', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_090_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    expect(document.querySelector('.pane-turnms')?.getAttribute('title'))
      .toBe('gui.ws.instance_turn_elapsed {"d":"1m30s"}')
  })
})

const quiet = (): Element | null => document.querySelector('.subagents-quiet')

describe('how long a working instance has been quiet', () => {
  it('says nothing while the run keeps showing signs of life', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_900_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000, lastSignalAtMs: 1_700_000_870_000 })} />)
    expect(shown()).toBe('15m00s')
    expect(quiet()).toBeNull()
  })

  it('says how long it has been quiet once a minute has passed without a sign', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_900_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000, lastSignalAtMs: 1_700_000_780_000 })} />)
    expect(quiet()?.textContent).toBe('gui.ws.instance_quiet {"d":"2m00s"}')
    expect(quiet()?.classList.contains('subagents-quiet-stalled')).toBe(false)
  })

  it('says no sign of life from ten minutes, the line the graph runner tells the main agent at', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_900_000)
    /* A run that hung on its first call: its last sign is its own start. */
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000, lastSignalAtMs: 1_700_000_000_000 })} />)
    expect(quiet()?.textContent).toBe('gui.ws.instance_stalled {"d":"15m00s"}')
    expect(quiet()?.classList.contains('subagents-quiet-stalled')).toBe(true)
    expect(quiet()?.getAttribute('title')).toBe('gui.ws.instance_stalled_title {"d":"15m00s"}')
  })

  it('guesses nothing from a server that does not publish the last sign', () => {
    wire()
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_900_000)
    render(<TurnClock row={row({ turnStartedAtMs: 1_700_000_000_000 })} />)
    expect(shown()).toBe('15m00s')
    expect(quiet()).toBeNull()
  })
})
