/* Components: what Rarus AI downloaded from its own server after it was installed -- memory,
   document reading, the browser, presentations, design, messenger bridges (app/rarus_app/components.py).
   The first start asks the same list on the window's start page; this card is where the answer is
   changed later. Required ones keep their switch on; switching one off removes it, switching one on
   downloads it, and either takes effect when the engine restarts. Only the window's shell can do any
   of it, so an ordinary browser gets a line saying where to go instead. */
import { useEffect, useRef, useState } from 'react'

import { code, t } from '../../../i18n/t'
import { shell } from '../../../lib/shell'
import { Card, Chip, InlineErr, Row, Rov, Switch } from '../Fields'

import type { ComponentItem, ComponentStatus, ComponentsView } from '../../../lib/shell'
import type { JSX } from 'react'

export const timing = { pollMs: 700 }

const mb = (n: number): string => String(Math.max(1, Math.round(n / 1e6)))
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

/* A switch moved, with what the components require kept true: on brings what it needs along, off
   takes down what needs it. */
export function flip(items: ComponentItem[], choice: Record<string, boolean>, id: string, on: boolean): Record<string, boolean> {
  const next = { ...choice, [id]: on }
  const item = items.find((i) => i.id === id)
  if (on && item) for (const need of item.requires) if (!next[need]) Object.assign(next, flip(items, next, need, true))
  if (!on) for (const dep of items.filter((i) => i.requires.includes(id))) if (next[dep.id]) Object.assign(next, flip(items, next, dep.id, false))
  return next
}

/* Whether the switches differ from what is on disk -- a required one that is missing (its first
   download failed) counts, so Apply is also how it is fetched again. */
export function dirty(items: ComponentItem[], choice: Record<string, boolean>): boolean {
  return items.some((i) => (i.setup === 'locked' ? !i.installed : !!choice[i.id] !== i.installed))
}

export function ComponentsCard(): JSX.Element | null {
  const [view, setView] = useState<ComponentsView | null>(null)
  const [windowed, setWindowed] = useState<boolean | null>(null)
  const [choice, setChoice] = useState<Record<string, boolean>>({})
  const [status, setStatus] = useState<ComponentStatus | null>(null)
  const [restart, setRestart] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(true)

  const load = async (): Promise<void> => {
    const api = await shell()
    if (!api || !api.components_view) {
      if (alive.current) setWindowed(false)
      return
    }
    const got = await api.components_view(code)
    if (!alive.current) return
    setWindowed(true)
    setView(got)
    setChoice(Object.fromEntries(got.items.map((i) => [i.id, i.on])))
  }

  useEffect(() => {
    alive.current = true
    void load().catch((e: unknown) => alive.current && setError(e instanceof Error ? e.message : String(e)))
    return () => { alive.current = false }
  }, [])

  const apply = async (): Promise<void> => {
    const api = await shell()
    if (!api || !api.components_install || !api.components_status || !view) return
    setError('')
    const removing = view.items.some((i) => i.installed && i.setup !== 'locked' && !choice[i.id])
    try {
      let got = await api.components_install(choice)
      setStatus(got)
      while (alive.current && got.state === 'working') {
        await sleep(timing.pollMs)
        got = await api.components_status()
        if (alive.current) setStatus(got)
      }
      if (!alive.current) return
      if (got.installed_any || removing) setRestart(true)
      await load()
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e))
    }
  }

  const restartNow = async (): Promise<void> => {
    const api = await shell()
    if (api && api.components_restart) await api.components_restart()
  }

  if (windowed === false) {
    return (
      <Card title={t('gui.settings.components.title')}>
        <Row label={t('gui.settings.components.title')} sub={t('gui.settings.components.browser_hint')} />
      </Card>
    )
  }
  if (!view || !view.items.length) return null

  const working = status?.state === 'working'
  const failed = status && status.state !== 'working' ? status.failed : []
  const names = (ids: string[]): string => ids.map((id) => view.items.find((i) => i.id === id)?.title ?? id).join(', ')
  return (
    <Card
      title={t('gui.settings.components.title')}
      act={
        <button type="button" className="mini" disabled={working || !dirty(view.items, choice)} onClick={() => void apply()}>
          {t('gui.settings.components.apply')}
        </button>
      }
    >
      <Row sub={t('gui.settings.components.lead')} />
      {view.items.map((item) => {
        const locked = item.setup === 'locked'
        return (
          <Row key={item.id} label={item.title} sub={`${item.about} · ${t('gui.settings.components.size', { n: mb(item.size) })}`}>
            {item.installed ? <Chip state="on">{t('gui.settings.components.installed')}</Chip> : null}
            {locked ? <Rov>{t('gui.settings.components.required')}</Rov> : null}
            <Switch on={locked || !!choice[item.id]} label={item.title} disabled={locked || working}
              onChange={(on) => setChoice(flip(view.items, choice, item.id, on))} />
          </Row>
        )
      })}
      {working && status ? (
        <Row sub={t('gui.settings.components.working', { got: mb(status.received), total: mb(status.total) })} />
      ) : null}
      {failed.length ? <InlineErr text={t('gui.settings.components.failed', { names: names(failed) })} /> : null}
      <InlineErr text={error} />
      {restart && !working ? (
        <Row sub={t('gui.settings.components.restart_note')}>
          <button type="button" className="mini" onClick={() => void restartNow()}>{t('gui.settings.components.restart')}</button>
        </Row>
      ) : null}
    </Card>
  )
}
