/* The served front end's persisted appearance preference and its DOM effect. */

export interface LookState {
  theme: string
  codeFont: string
  motion: string
}

const KEY = 'raven.gui.look'
/* Dark until the reader picks otherwise: the owner's choice for every browser
   that has not stored one, the household's tablets included. */
const DEFAULT: LookState = { theme: 'dark', codeFont: 'system', motion: 'on' }

/* The system's own "reduce motion" is the starting point for a reader who has not chosen: spec 10.4,
   item 6. A stored choice, either way, wins over it. */
const systemMotion = (): string => {
  try {
    return matchMedia('(prefers-reduced-motion: reduce)').matches ? 'off' : DEFAULT.motion
  } catch {
    return DEFAULT.motion
  }
}

let state: LookState = { ...DEFAULT }
/* Whether the reader picked motion themselves: only then is it stored, so a theme change does not
   freeze the system's setting as a choice. */
let motionChosen = false

type NativeWindow = Window & {
  webkit?: { messageHandlers?: { raven?: { postMessage(value: unknown): void } } }
}

function tellNativeTheme(): void {
  const value = document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  try {
    const native = window as NativeWindow
    native.webkit?.messageHandlers?.raven?.postMessage({ type: 'theme', value })
  } catch {
    /* The browser host may expose a partial native bridge. */
  }
}

function apply(): void {
  const data = document.documentElement.dataset
  if (state.theme === 'system') delete data.theme
  else data.theme = state.theme
  data.motion = state.motion
  data.font = state.codeFont
  tellNativeTheme()
}

function save(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...state, motion: motionChosen ? state.motion : undefined }))
  } catch {
    /* Storage may be unavailable in private mode or over quota. */
  }
}

export function load(): void {
  state = { ...DEFAULT, motion: systemMotion() }
  motionChosen = false
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(KEY) || 'null')
    if (raw && typeof raw === 'object') {
      const value = raw as Partial<LookState>
      if (value.theme) state.theme = value.theme
      if (value.codeFont) state.codeFont = value.codeFont
      if (value.motion) {
        state.motion = value.motion
        motionChosen = true
      }
    }
  } catch {
    /* The defaults are the fallback for malformed or unavailable storage. */
  }
  apply()
}

export const get = (): LookState => ({ ...state })

export function set(patch: Partial<LookState>): void {
  if (patch.theme) state.theme = patch.theme
  if (patch.codeFont) state.codeFont = patch.codeFont
  if (patch.motion) {
    state.motion = patch.motion
    motionChosen = true
  }
  apply()
  save()
}

/* Test seam only: the theme the reader picked is the module's. */
export function _resetForTests(): void {
  state = { ...DEFAULT }
  motionChosen = false
}
