/* The update row of the account card: ask the window's shell whether a newer Rarus AI is out, and
   if so let it fetch the installer (the shell checks its SHA-256), run it and close the program.
   The shell owns every step; the page only asks, shows the percentage and reports a refusal. An
   ordinary browser has no shell, so there the button is off and the row says where to update. */
import { useEffect, useRef, useState } from 'react'

import { t } from '../../../i18n/t'
import { shell } from '../../../lib/shell'
import { Row, Rov } from '../Fields'

import type { JSX } from 'react'

export const timing = { pollMs: 700 }

type Phase =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'current' }
  | { kind: 'available'; version: string }
  | { kind: 'downloading'; percent: number | null }
  | { kind: 'restarting' }
  | { kind: 'error'; text: string }

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))
const message = (e: unknown): string => (e instanceof Error ? e.message : String(e))

function phaseText(phase: Phase): string {
  switch (phase.kind) {
    case 'checking': return t('gui.settings.account.update_checking')
    case 'current': return t('gui.settings.account.update_current')
    case 'available': return t('gui.settings.account.update_available', { v: phase.version })
    case 'downloading': return phase.percent === null ? t('gui.settings.account.update_downloading_plain') : t('gui.settings.account.update_downloading', { n: phase.percent })
    case 'restarting': return t('gui.settings.account.update_restarting')
    case 'error': return t('gui.settings.account.update_failed', { e: phase.text })
    default: return ''
  }
}

export function UpdateRow({ windowed }: { windowed: boolean | null }): JSX.Element {
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })
  const [current, setCurrent] = useState('')
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    void shell().then(async (api) => {
      if (!api || !alive.current) return
      try {
        const status = await api.update_status()
        if (alive.current) setCurrent(status.current)
      } catch {
        /* the version is a nicety; the buttons still work without it */
      }
    })
    return () => { alive.current = false }
  }, [])

  const check = async (): Promise<void> => {
    const api = await shell()
    if (!api) return
    setPhase({ kind: 'checking' })
    try {
      const got = await api.update_check()
      if (!alive.current) return
      setCurrent(got.current)
      if (got.error) setPhase({ kind: 'error', text: got.error })
      else setPhase(got.available && got.version ? { kind: 'available', version: got.version } : { kind: 'current' })
    } catch (e) {
      if (alive.current) setPhase({ kind: 'error', text: message(e) })
    }
  }

  const install = async (): Promise<void> => {
    const api = await shell()
    if (!api) return
    setPhase({ kind: 'downloading', percent: null })
    try {
      let status = await api.update_start()
      while (alive.current && (status.state === 'checking' || status.state === 'downloading')) {
        setPhase({ kind: 'downloading', percent: status.percent })
        await sleep(timing.pollMs)
        status = await api.update_status()
      }
      if (!alive.current) return
      if (status.state === 'ready') {
        setPhase({ kind: 'restarting' })
        const applied = await api.update_apply()
        if (!applied.ok && alive.current) setPhase({ kind: 'error', text: applied.error ?? '' })
      } else if (status.state === 'current') {
        setPhase({ kind: 'current' })
      } else {
        setPhase({ kind: 'error', text: status.error ?? '' })
      }
    } catch (e) {
      if (alive.current) setPhase({ kind: 'error', text: message(e) })
    }
  }

  const busy = phase.kind === 'checking' || phase.kind === 'downloading' || phase.kind === 'restarting'
  const sub = windowed === false
    ? t('gui.settings.account.update_hint')
    : current ? t('gui.settings.account.updates_sub', { v: current }) : undefined

  return (
    <Row label={t('gui.settings.account.updates')} sub={sub}>
      {phase.kind !== 'idle' ? <Rov>{phaseText(phase)}</Rov> : null}
      {phase.kind === 'available' ? (
        <button type="button" className="mini" onClick={() => void install()}>
          {t('gui.settings.account.update_install')}
        </button>
      ) : (
        <button type="button" className="mini ghost" disabled={windowed === false || busy} onClick={() => void check()}>
          {t('gui.settings.account.update_check')}
        </button>
      )}
    </Row>
  )
}
