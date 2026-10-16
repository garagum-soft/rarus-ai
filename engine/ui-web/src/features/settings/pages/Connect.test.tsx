// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { chosen } from './Connect'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toasts = vi.hoisted(() => ({ calls: [] as string[] }))
vi.mock('../../../state/toast', () => ({ show: (t: string) => { toasts.calls.push(t) }, subscribe: () => () => {}, get: () => [] }))

const pre = (): string[] => Array.from(document.querySelectorAll('.settings-connect-pre')).map((el) => el.textContent || '')

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
  toasts.calls = []
})

describe('connect an agent', () => {
  it('shows the first client\'s command and the agent\'s instructions on open', async () => {
    install()
    await mount('connect')
    expect(screen.getByText('gui.settings.connect.run')).toBeTruthy()
    expect(pre()).toEqual(['claude mcp add raven -- raven mcp serve', 'You are connected to Raven.'])
  })

  it('a client pick shows that client\'s file and its content', async () => {
    install()
    await mount('connect')
    await act(async () => { fireEvent.click(screen.getByText('Cursor')) })
    expect(screen.getByText('gui.settings.connect.file {"file":".cursor/mcp.json"}')).toBeTruthy()
    expect(pre()[0]).toBe('{"mcpServers": {"raven": {}}}')
    expect(screen.getByText('Cursor').getAttribute('aria-pressed')).toBe('true')
  })

  it('copies the command as shown', async () => {
    const written: string[] = []
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (text: string) => { written.push(text) } }, configurable: true,
    })
    install()
    await mount('connect')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.connect.copy')[0]!) })
    expect(written).toEqual(['claude mcp add raven -- raven mcp serve'])
  })

  it('says the gateway did not answer, and the retry asks again', async () => {
    let n = 0
    install(undefined, {
      mcpClients: async () => {
        n += 1
        if (n === 1) throw new Error('down')
        return [{ client: 'codex', label: 'Codex', kind: 'cli', connect: 'codex mcp add raven -- raven mcp serve', filename: '', instructions: 'hi' }]
      },
    })
    await mount('connect')
    expect(screen.getByText('gui.settings.connect.none')).toBeTruthy()
    expect(toasts.calls[0]).toContain('down')
    await act(async () => { fireEvent.click(screen.getByText('gui.plug.retry')) })
    expect(pre()[0]).toBe('codex mcp add raven -- raven mcp serve')
  })

  it('falls back to the first client when the picked one is gone', () => {
    const rows = [{ client: 'a', label: 'A', kind: 'cli', connect: 'x', filename: '', instructions: '' }]
    expect(chosen(rows, 'gone')!.client).toBe('a')
    expect(chosen([], null)).toBeNull()
  })
})
