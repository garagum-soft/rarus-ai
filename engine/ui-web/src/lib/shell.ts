/* The desktop window around the page, when there is one.
 *
 * Inside the Rarus AI window the shell hands the page a small bridge
 * (`window.pywebview.api`): first-run bookkeeping and the one setting only the
 * shell can change, the Windows autostart entry. In an ordinary browser there
 * is no bridge and `shell()` answers null -- every caller treats that as "not
 * a desktop window", never as a failure. No secret crosses the bridge. */

export interface ShellApi {
  first_run(): Promise<boolean>
  onboarded(): Promise<void>
  autostart_enabled(): Promise<boolean>
  set_autostart(enabled: boolean): Promise<boolean>
  sign_out(): Promise<unknown>
}

type Host = Window & { pywebview?: { api?: Partial<ShellApi> } }

/* How long to wait for a bridge the window has announced but not yet filled in. */
const READY_MS = 2000

const present = (api: Partial<ShellApi> | undefined): api is ShellApi => !!api && typeof api.first_run === 'function'

export function shell(): Promise<ShellApi | null> {
  const host = window as Host
  if (!host.pywebview) return Promise.resolve(null)
  if (present(host.pywebview.api)) return Promise.resolve(host.pywebview.api)
  return new Promise((resolve) => {
    const done = (): void => {
      window.removeEventListener('pywebviewready', done)
      clearTimeout(timer)
      const api = (window as Host).pywebview?.api
      resolve(present(api) ? api : null)
    }
    const timer = setTimeout(done, READY_MS)
    window.addEventListener('pywebviewready', done)
  })
}

/** Whether the shell says this is the first start (no wizard has been finished). False outside the window. */
export async function firstRun(): Promise<boolean> {
  try {
    const api = await shell()
    return api ? !!(await api.first_run()) : false
  } catch {
    return false
  }
}
