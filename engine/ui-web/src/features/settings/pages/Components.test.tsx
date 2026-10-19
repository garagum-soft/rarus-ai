// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../../i18n/t'
import { ComponentsCard, dirty, flip, timing } from './Components'

import type { ComponentItem, ComponentStatus, ComponentsView, ShellApi } from '../../../lib/shell'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const bridge = vi.hoisted(() => ({ api: null as null | Partial<ShellApi> }))
vi.mock('../../../lib/shell', () => ({ shell: async () => bridge.api }))

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
})

afterEach(() => {
  cleanup()
  resetTranslator()
  bridge.api = null
})

const item = (id: string, setup: ComponentItem['setup'], installed: boolean, on: boolean, requires: string[] = []): ComponentItem => ({
  id, setup, title: id.toUpperCase(), about: 'about ' + id, size: 52_000_000, unpacked: 0, requires, installed, on,
})

const ITEMS = [
  item('memory', 'locked', true, true),
  item('browser', 'optional', true, true),
  item('design', 'optional', false, false, ['browser']),
  item('messengers', 'off', false, false),
]

const idle: ComponentStatus = { state: 'idle', items: [], received: 0, total: 0, failed: [], installed_any: false }

function shellWith(items: ComponentItem[], after?: Partial<ComponentStatus>): { api: Partial<ShellApi>; installs: Record<string, boolean>[] } {
  const installs: Record<string, boolean>[] = []
  let polls = 0
  const done: ComponentStatus = { ...idle, state: 'done', installed_any: true, ...after }
  const api: Partial<ShellApi> = {
    components_view: vi.fn(async (): Promise<ComponentsView> => ({ pending: false, items, hidden_bytes: 0, status: idle })),
    components_install: vi.fn(async (choices: Record<string, boolean>) => {
      installs.push(choices)
      return { ...idle, state: 'working', received: 10_000_000, total: 52_000_000 } as ComponentStatus
    }),
    components_status: vi.fn(async () => (++polls > 1 ? done : { ...idle, state: 'working', received: 30_000_000, total: 52_000_000 } as ComponentStatus)),
    components_restart: vi.fn(async () => ({ ok: true })),
  }
  return { api, installs }
}

describe('the components card', () => {
  it('keeps what components require true in both directions', () => {
    const choice = { memory: true, browser: true, design: true, messengers: false }
    expect(flip(ITEMS, choice, 'browser', false)).toEqual({ ...choice, browser: false, design: false })
    expect(flip(ITEMS, { ...choice, browser: false, design: false }, 'design', true)).toEqual(choice)
  })

  it('knows when the switches differ from what is installed', () => {
    expect(dirty(ITEMS, { memory: true, browser: true, design: false, messengers: false })).toBe(false)
    expect(dirty(ITEMS, { memory: true, browser: true, design: true, messengers: false })).toBe(true)
    const missing = [item('memory', 'locked', false, true), ...ITEMS.slice(1)]
    expect(dirty(missing, { memory: true, browser: true, design: false, messengers: false })).toBe(true)
  })

  it('lists the components, keeps the required one fixed, applies the switches and offers the restart', async () => {
    timing.pollMs = 1
    const { api, installs } = shellWith(ITEMS)
    bridge.api = api
    await act(async () => { render(<ComponentsCard />) })

    expect(screen.getByText('MEMORY')).toBeTruthy()
    expect(screen.getByText('gui.settings.components.required')).toBeTruthy()
    const switches = screen.getAllByRole('switch')
    expect(switches.map((s) => (s as HTMLButtonElement).disabled)).toEqual([true, false, false, false])

    const apply = screen.getByText('gui.settings.components.apply') as HTMLButtonElement
    expect(apply.disabled).toBe(true)
    await act(async () => { fireEvent.click(switches[3]!) })
    expect(apply.disabled).toBe(false)

    await act(async () => { fireEvent.click(apply) })
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
    expect(installs).toEqual([{ memory: true, browser: true, design: false, messengers: true }])
    const restart = await screen.findByText('gui.settings.components.restart')
    await act(async () => { fireEvent.click(restart) })
    expect(api.components_restart).toHaveBeenCalledTimes(1)
  })

  it('names what failed', async () => {
    timing.pollMs = 1
    const { api } = shellWith(ITEMS, { failed: ['design'], installed_any: false })
    bridge.api = api
    await act(async () => { render(<ComponentsCard />) })
    await act(async () => { fireEvent.click(screen.getAllByRole('switch')[2]!) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.components.apply')) })
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
    expect(screen.getByRole('alert').textContent).toContain('DESIGN')
  })

  it('says where to go in an ordinary browser', async () => {
    await act(async () => { render(<ComponentsCard />) })
    expect(screen.getByText('gui.settings.components.browser_hint')).toBeTruthy()
  })

  it('draws nothing for a window whose shell lists no components', async () => {
    bridge.api = shellWith([]).api
    const { container } = render(<ComponentsCard />)
    await act(async () => { await Promise.resolve() })
    expect(container.textContent).toBe('')
  })
})
