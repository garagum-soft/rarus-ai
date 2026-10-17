// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { TOP_UP_URL, balanceText, errorText } from './Account'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toasts = vi.hoisted(() => ({ values: [] as string[] }))
vi.mock('../../../state/toast', () => ({ show: (v: string) => { toasts.values.push(v) }, subscribe: () => () => {}, get: () => [] }))
const opened = vi.hoisted(() => ({ values: [] as string[] }))
vi.mock('../../../lib/openUrl', () => ({ open: (v: string) => { opened.values.push(v) } }))
const bridge = vi.hoisted(() => ({ api: null as null | { sign_out: () => Promise<unknown> } }))
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
