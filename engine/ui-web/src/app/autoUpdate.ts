/* The program updates itself (owner, 2026-10-19: "like Alem AI: a popup says a new version is out, and if
 * you press nothing and no work is running, it updates by itself in ten minutes").
 *
 * Only inside the Rarus AI window, whose shell does the real work (lib/shell.ts, app/rarus_app): a minute
 * after start and then every ten minutes the page asks the shell to look for a newer version; the shell
 * downloads it in the background and checks its SHA-256. Once it is ready the page asks once per version
 * per run, in the app's own dialog: update now, or cancel. Unanswered, it installs itself ten minutes
 * later -- but only when no agent turn is running and the reader has not touched the window for a minute,
 * so nothing that is being done is cut off. Cancel means "not this run": the next start asks again.
 */

import { turn } from '../features/composer/mount'
import { t } from '../i18n/t'
import { shell } from '../lib/shell'
import { gateway } from '../rpc/gateway'
import * as confirm from '../state/confirm'
import { show as toast } from '../state/toast'

import type { ShellApi } from '../lib/shell'

export const timing = {
  /* The first look, after the window has settled. */
  firstMs: 60_000,
  /* How often the shell is asked again. */
  everyMs: 10 * 60_000,
  /* From the question to installing by itself. */
  waitMs: 10 * 60_000,
  /* How often the page looks at the download and at the clock. */
  tickMs: 15_000,
  /* How long the reader has to have left the window alone. */
  quietMs: 60_000,
}

interface State {
  api: ShellApi | null
  /* The version already asked about this run, and whether the reader said not now. */
  asked: string
  declined: boolean
  deadline: number
  lastInput: number
  lastCheck: number
  applying: boolean
  timer: ReturnType<typeof setInterval> | null
}

const state: State = { api: null, asked: '', declined: false, deadline: 0, lastInput: 0, lastCheck: 0, applying: false, timer: null }

const now = (): number => Date.now()

const touched = (): void => { state.lastInput = now() }

/* Any turn in flight, in this conversation or another one: then the update waits. */
async function busy(): Promise<boolean> {
  if (turn.busy()) return true
  try {
    const r = await gateway().call('session.list', { limit: 50 })
    return r.sessions.some((s) => !!s.running)
  } catch {
    /* No answer is no evidence of idleness: wait for the next tick. */
    return true
  }
}

async function install(): Promise<void> {
  const api = state.api
  if (!api || state.applying) return
  state.applying = true
  try {
    const r = await api.update_apply()
    if (!r.ok) {
      state.applying = false
      toast(t('gui.autoupd.failed', { e: r.error ?? '' }))
    }
    /* On success the shell closes the window and the installer starts the new version. */
  } catch (e) {
    state.applying = false
    toast(t('gui.autoupd.failed', { e: (e as Error).message || String(e) }))
  }
}

const ourQuestion = (): boolean => confirm.get().open && confirm.get().title === t('gui.autoupd.title', { v: state.asked })

/** One look: start a check when it is due, ask when a download is ready, install when the wait is over. */
export async function tick(): Promise<void> {
  const api = state.api
  if (!api || state.applying) return
  let status = await api.update_status()
  if (['idle', 'current', 'error'].includes(status.state) && now() - state.lastCheck >= timing.everyMs) {
    state.lastCheck = now()
    status = await api.update_start()
  }
  if (status.state !== 'ready' || !status.version) return
  if (state.asked !== status.version) {
    /* Another question is up (a delete, a sign-out): ours waits for the sheet. */
    if (confirm.get().open) return
    state.asked = status.version
    state.declined = false
    state.deadline = now() + timing.waitMs
    confirm.ask(t('gui.autoupd.title', { v: status.version }), t('gui.autoupd.body'), t('gui.autoupd.now'), () => { void install() }, 'primary')
    return
  }
  if (!ourQuestion()) {
    /* Closed without "update now": the reader said not this run. */
    state.declined = true
    return
  }
  if (state.declined || now() < state.deadline) return
  if (now() - state.lastInput < timing.quietMs) return
  if (await busy()) return
  confirm.answer(true)
}

/** Starts the watch inside the desktop window; a plain browser has no shell and nothing starts. */
export async function start(): Promise<void> {
  if (state.timer) return
  const api = await shell()
  if (!api || typeof api.update_start !== 'function') return
  state.api = api
  state.lastCheck = now() - timing.everyMs + timing.firstMs
  for (const kind of ['pointerdown', 'keydown', 'wheel'] as const) window.addEventListener(kind, touched, { passive: true })
  state.timer = setInterval(() => { void tick().catch(() => {}) }, timing.tickMs)
}

/** Test seam only. */
export function _resetForTests(api: ShellApi | null = null): void {
  if (state.timer) clearInterval(state.timer)
  for (const kind of ['pointerdown', 'keydown', 'wheel'] as const) window.removeEventListener(kind, touched)
  Object.assign(state, { api, asked: '', declined: false, deadline: 0, lastInput: 0, lastCheck: 0, applying: false, timer: null })
}
