// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { TOP_UP_URL, balanceText, errorText } from './Account'
import { timing } from './Updates'

import type { ShellApi } from '../../../lib/shell'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toasts = vi.hoisted(() => ({ values: [] as string[] }))
vi.mock('../../../state/toast', () => ({ show: (v: string) => { toasts.values.push(v) }, subscribe: () => () => {}, get: () => [] }))
const opened = vi.hoisted(() => ({ values: [] as string[] }))
vi.mock('../../../lib/openUrl', () => ({ open: (v: string) => { opened.values.push(v) } }))
const bridge = vi.hoisted(() => ({ api: null as null | Partial<ShellApi> }))
vi.mock('../../../lib/shell', () => ({ shell: async () => bridge.api }))

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
  toasts.values = []
  opened.values = []
  bridge.api = null
})

describe('account page', () => {
  it('shows the signed-in email and the balance', async () => {
    const { calls } = install()
    await mount('account')
    expect(await screen.findByText('me@example.com')).toBeTruthy()
    expect(screen.getByText(/^gui.settings.account.credits .*1.200/)).toBeTruthy()
    expect(calls.filter((c) => c[0] === 'account')).toHaveLength(1)
  })

  it('asks again on Refresh', async () => {
    const { calls } = install()
    await mount('account')
    await screen.findByText('me@example.com')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.refresh')) })
    expect(calls.filter((c) => c[0] === 'account')).toHaveLength(2)
  })

  it('says there is no balance rather than showing zero when the server gave none', () => {
    expect(balanceText({ signed_in: true, email: 'a@b.c', balance: null, error: '' })).toBe('gui.settings.account.balance_unknown')
    expect(balanceText({ signed_in: true, email: 'a@b.c', balance: 0, error: '' })).toContain('"n":"0"')
  })

  it('shows what went wrong when the account cannot be read', async () => {
    install(snap(), { account: async () => { throw new Error('engine down') } })
    await mount('account')
    expect(await screen.findByText('engine down')).toBeTruthy()
    expect(screen.getByText('gui.settings.account.signed_out')).toBeTruthy()
  })

  it('opens the site to top up', async () => {
    install()
    await mount('account')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.top_up_open')) })
    expect(opened.values).toEqual([TOP_UP_URL])
  })

  it('signs out through the window when there is one', async () => {
    const signOut = vi.fn(async () => undefined)
    bridge.api = { sign_out: signOut }
    install()
    await mount('account')
    await screen.findByText('me@example.com')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.sign_out_do')) })
    expect(signOut).toHaveBeenCalledTimes(1)
  })

  it('leaves signing out to the window in an ordinary browser', async () => {
    install()
    await mount('account')
    await screen.findByText('me@example.com')
    const button = screen.getByText('gui.settings.account.sign_out_do') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(screen.getAllByText('gui.settings.account.sign_out_hint').length).toBeGreaterThan(0)
  })

  it('says so when the window refuses to sign out', async () => {
    bridge.api = { sign_out: vi.fn(async () => { throw new Error('no') }) }
    install()
    await mount('account')
    await screen.findByText('me@example.com')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.sign_out_do')) })
    expect(toasts.values).toEqual(['gui.settings.account.sign_out_failed'])
  })
})

describe('account errors', () => {
  it('words each code the engine can answer with, and shows any other text as it came', async () => {
    for (const code of ['unauthorized', 'no_credits', 'blocked', 'network', 'server']) {
      expect(errorText(code)).toBe(`gui.settings.account.err_${code}`)
    }
    expect(errorText('engine down')).toBe('engine down')
  })

  it('shows the worded error under the card', async () => {
    install(snap(), { account: async () => ({ signed_in: true, email: 'me@example.com', balance: null, error: 'network' }) })
    await mount('account')
    expect(await screen.findByText('gui.settings.account.err_network')).toBeTruthy()
  })
})

describe('update row', () => {
  const status = (over: Record<string, unknown> = {}): ShellApi['update_status'] => async () => ({ state: 'idle', current: '0.1.0', percent: null, ...over }) as Awaited<ReturnType<ShellApi['update_status']>>

  beforeEach(() => { timing.pollMs = 1 })

  const open = async (api: Partial<ShellApi> | null): Promise<void> => {
    bridge.api = api
    install()
    await mount('account')
    await screen.findByText('me@example.com')
  }

  it('says which version is installed and that it is the latest', async () => {
    await open({ update_status: status(), update_check: async () => ({ available: false, current: '0.1.0' }) })
    expect(await screen.findByText(/^gui.settings.account.updates_sub .*0\.1\.0/)).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.update_check')) })
    expect(await screen.findByText('gui.settings.account.update_current')).toBeTruthy()
  })

  it('offers the install of a newer version, follows the download and runs the installer', async () => {
    const polls = [{ state: 'downloading', percent: 40 }, { state: 'ready', percent: 100 }]
    const apply = vi.fn(async () => ({ ok: true, version: '0.2.0' }))
    await open({
      update_status: async () => ({ state: 'idle', current: '0.1.0', percent: null }),
      update_check: async () => ({ available: true, current: '0.1.0', version: '0.2.0' }),
      update_start: async () => ({ state: 'downloading', current: '0.1.0', percent: null, version: '0.2.0' }),
      update_apply: apply,
    })
    const status = vi.spyOn(bridge.api as ShellApi, 'update_status').mockImplementation(async () => ({ current: '0.1.0', ...(polls.shift() ?? { state: 'ready', percent: 100 }) }) as never)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.update_check')) })
    expect(await screen.findByText(/^gui.settings.account.update_available .*0\.2\.0/)).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.update_install')) })
    expect(await screen.findByText('gui.settings.account.update_restarting')).toBeTruthy()
    expect(apply).toHaveBeenCalledTimes(1)
    expect(status).toHaveBeenCalled()
  })

  it('shows the reason when the installer is refused', async () => {
    await open({
      update_status: status(),
      update_check: async () => ({ available: true, current: '0.1.0', version: '0.2.0' }),
      update_start: async () => ({ state: 'ready', current: '0.1.0', percent: 100 }),
      update_apply: async () => ({ ok: false, error: 'changed on disk' }),
    })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.update_check')) })
    await act(async () => { fireEvent.click(await screen.findByText('gui.settings.account.update_install')) })
    expect(await screen.findByText(/^gui.settings.account.update_failed .*changed on disk/)).toBeTruthy()
  })

  it('shows the reason when the download fails', async () => {
    await open({
      update_status: status(),
      update_check: async () => ({ available: true, current: '0.1.0', version: '0.2.0' }),
      update_start: async () => ({ state: 'error', current: '0.1.0', percent: null, error: 'does not match its sha256' }),
    })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.update_check')) })
    await act(async () => { fireEvent.click(await screen.findByText('gui.settings.account.update_install')) })
    expect(await screen.findByText(/^gui.settings.account.update_failed .*sha256/)).toBeTruthy()
  })

  it('shows why a check could not be made', async () => {
    await open({ update_status: status(), update_check: async () => ({ available: false, current: '0.1.0', error: 'offline' }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.account.update_check')) })
    expect(await screen.findByText(/^gui.settings.account.update_failed .*offline/)).toBeTruthy()
  })

  it('is switched off in an ordinary browser, which has no shell to update', async () => {
    await open(null)
    const button = screen.getByText('gui.settings.account.update_check') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(screen.getAllByText('gui.settings.account.update_hint').length).toBeGreaterThan(0)
  })
})
