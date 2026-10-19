// @vitest-environment happy-dom
/* The desktop window updates itself: a ready download is asked about once, and
 * unanswered it installs ten minutes later -- only while no turn runs and the
 * reader has left the window alone. Cancel keeps this run on the old version.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { t } from '../i18n/t'
import * as confirm from '../state/confirm'
import * as toast from '../state/toast'

import type { ShellApi, UpdateStatus } from '../lib/shell'

const env = vi.hoisted(() => ({
  api: null as unknown,
  busy: false,
  running: false,
}))

vi.mock('../lib/shell', () => ({ shell: () => Promise.resolve(env.api) }))
vi.mock('../features/composer/mount', () => ({ turn: { busy: () => env.busy } }))
vi.mock('../rpc/gateway', () => ({
  gateway: () => ({ call: () => Promise.resolve({ sessions: [{ running: env.running }] }) }),
}))

import { _resetForTests, start, timing } from './autoUpdate'

interface Fake {
  api: ShellApi
  status: UpdateStatus
  started: number
  applied: number
  applyAnswer: { ok: boolean; error?: string }
}

/* A shell whose check finds 0.1.4 and has it downloaded by the next look. */
function fakeShell(): Fake {
  const fake: Fake = {
    status: { state: 'idle', current: '0.1.3', percent: null },
    started: 0,
    applied: 0,
    applyAnswer: { ok: true, version: '0.1.4' } as { ok: boolean },
    api: null as unknown as ShellApi,
  }
  fake.api = {
    first_run: () => Promise.resolve(false),
    onboarded: () => Promise.resolve(),
    autostart_enabled: () => Promise.resolve(false),
    set_autostart: () => Promise.resolve(false),
    sign_out: () => Promise.resolve(null),
    update_check: () => Promise.resolve({ available: false, current: '0.1.3' }),
    update_start: () => {
      fake.started += 1
      fake.status = { state: 'checking', current: '0.1.3', percent: null }
      return Promise.resolve(fake.status)
    },
    update_status: () => {
      if (fake.status.state === 'checking') fake.status = { state: 'ready', current: '0.1.3', percent: 100, version: '0.1.4' }
      return Promise.resolve(fake.status)
    },
    update_apply: () => {
      fake.applied += 1
      return Promise.resolve(fake.applyAnswer)
    },
  }
  return fake
}

const title = (): string => t('gui.autoupd.title', { v: '0.1.4' })

async function after(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms)
}

/* Started, and through the first look: the question is up. */
async function asked(fake: Fake): Promise<void> {
  env.api = fake.api
  await start()
  await after(timing.firstMs + timing.tickMs)
  expect(confirm.get().open).toBe(true)
  expect(confirm.get().title).toBe(title())
}

describe('the self-update of the desktop window', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    confirm._resetForTests()
    toast._resetForTests()
    env.api = null
    env.busy = false
    env.running = false
    _resetForTests()
  })

  afterEach(() => {
    _resetForTests()
    vi.useRealTimers()
  })

  it('does nothing in a plain browser, where there is no shell', async () => {
    await start()
    await after(timing.everyMs * 3)
    expect(confirm.get().open).toBe(false)
  })

  it('checks a minute after start, not before, and then every ten minutes', async () => {
    const fake = fakeShell()
    fake.api.update_status = () => Promise.resolve(fake.status)
    fake.api.update_start = () => {
      fake.started += 1
      fake.status = { state: 'current', current: '0.1.3', percent: null }
      return Promise.resolve(fake.status)
    }
    env.api = fake.api
    await start()
    await after(timing.firstMs - timing.tickMs)
    expect(fake.started).toBe(0)
    await after(timing.tickMs)
    expect(fake.started).toBe(1)
    await after(timing.everyMs)
    expect(fake.started).toBe(2)
  })

  it('asks once the download is ready and installs by itself ten minutes later when nothing is going on', async () => {
    const fake = fakeShell()
    await asked(fake)
    expect(confirm.get().label).toBe(t('gui.autoupd.now'))
    expect(confirm.get().tone).toBe('primary')
    await after(timing.waitMs - timing.tickMs * 2)
    expect(fake.applied).toBe(0)
    await after(timing.tickMs * 2)
    expect(fake.applied).toBe(1)
    expect(confirm.get().open).toBe(false)
  })

  it('installs at once on "update now"', async () => {
    const fake = fakeShell()
    await asked(fake)
    confirm.answer(true)
    await after(0)
    expect(fake.applied).toBe(1)
  })

  it('waits while a turn runs here or in another conversation', async () => {
    const fake = fakeShell()
    await asked(fake)
    env.busy = true
    await after(timing.waitMs + timing.tickMs)
    expect(fake.applied).toBe(0)
    env.busy = false
    env.running = true
    await after(timing.tickMs * 4)
    expect(fake.applied).toBe(0)
    env.running = false
    await after(timing.tickMs)
    expect(fake.applied).toBe(1)
  })

  it('waits while the reader is using the window', async () => {
    const fake = fakeShell()
    await asked(fake)
    await after(timing.waitMs - timing.tickMs)
    window.dispatchEvent(new Event('keydown'))
    await after(timing.tickMs * 2)
    expect(fake.applied).toBe(0)
    await after(timing.quietMs)
    expect(fake.applied).toBe(1)
  })

  it('cancel keeps the old version for this run and does not ask again', async () => {
    const fake = fakeShell()
    await asked(fake)
    confirm.answer(false)
    await after(timing.waitMs * 3)
    expect(fake.applied).toBe(0)
    expect(confirm.get().open).toBe(false)
  })

  it('lets another question finish before asking its own', async () => {
    const fake = fakeShell()
    env.api = fake.api
    confirm.ask('Delete?', '', 'Delete', () => {})
    await start()
    await after(timing.firstMs + timing.tickMs * 2)
    expect(confirm.get().title).toBe('Delete?')
    confirm.answer(false)
    await after(timing.tickMs)
    expect(confirm.get().title).toBe(title())
  })

  it('says why when the installer cannot be started, and keeps the program running', async () => {
    const fake = fakeShell()
    fake.applyAnswer = { ok: false, error: 'checksum mismatch' }
    document.body.innerHTML = '<div id="toasts"></div>'
    await asked(fake)
    confirm.answer(true)
    await after(0)
    expect(fake.applied).toBe(1)
    expect(toast.get().map((i) => i.text)).toContain(t('gui.autoupd.failed', { e: 'checksum mismatch' }))
  })
})
