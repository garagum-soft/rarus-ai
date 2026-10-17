// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import * as page from '../../state/page'
import { resetSources, setSources } from '../../state/sources'
import { CanvasApp } from './CanvasPage'
import * as store from './store'

import type { AgentNode, OrgRow } from './graph'
import type { CanvasSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const node = (name: string, over: Partial<AgentNode> = {}): AgentNode => ({
  name, origin: 'agent', role: '', description: '', model: '', ready: true, ...over,
})
const row = (name: string, reports_to: string | null = null): OrgRow => ({ name, reports_to, department: '', title: '' })

interface Harness {
  chart: OrgRow[]
  prompts: Map<string, string>
  source: CanvasSource
}

function install(): Harness {
  const h: Harness = {
    chart: [row('raven'), row('coder', 'raven')],
    prompts: new Map([['ana', 'be kind']]),
    source: null as unknown as CanvasSource,
  }
  const agents = [
    node('raven', { description: 'the head' }),
    node('coder'),
    node('writer'),
    node('ana', { origin: 'employee', role: 'Analyst', model: 'm-1' }),
  ]
  h.source = {
    load: async () => ({ agents, org: h.chart.map((r) => ({ ...r })), maxDepth: 6 }),
    saveOrg: vi.fn(async (rows: OrgRow[]) => {
      h.chart = rows.map((r) => ({ ...r }))
      return h.chart.map((r) => ({ ...r }))
    }),
    promptGet: async (name: string) => h.prompts.get(name) ?? '',
    promptSet: async (name: string, text: string) => {
      h.prompts.set(name, text)
      return true
    },
  }
  setTranslator((key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key))
  setSources({ canvas: h.source })
  return h
}

const host = (): HTMLElement => document.getElementById('canvasBody')!
const mount = (): ReturnType<typeof render> => render(<CanvasApp />, { container: host() })
const cards = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('[data-card]')]
const card = (name: string): HTMLElement => document.querySelector<HTMLElement>(`[data-card="${name}"]`)!
const stage = (): HTMLElement => document.querySelector<HTMLElement>('[role="tree"]')!
const tool = (key: string): HTMLButtonElement =>
  [...document.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent === key || b.getAttribute('aria-label') === key)!

const openPage = async (): Promise<void> => {
  await act(async () => {
    store.open()
    await Promise.resolve()
  })
  await vi.waitFor(() => expect(store.get().loaded).toBe(true))
}
const press = async (el: Element, key: string, init: KeyboardEventInit = {}): Promise<void> => {
  await act(async () => {
    fireEvent.keyDown(el, { key, ...init })
  })
}

beforeEach(() => {
  localStorage.clear()
  document.body.innerHTML =
    '<div class="app"></div>' +
    ['extAgentsPage', 'connectionsPage', 'knowledgePage']
      .map((id) => `<section class="page" id="${id}" data-open="false"><div class="work"></div></section>`).join('') +
    '<section class="page" id="canvasPage" data-open="false"><div class="work"><div id="canvasBody"></div></div></section>' +
    '<aside class="detail" id="detail" data-open="false"></aside>'
})

afterEach(() => {
  cleanup()
  page.show(null)
  page._resetForTests()
  confirm._resetForTests()
  store._resetForTests()
  resetSources()
  resetTranslator()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('the canvas island', () => {
  it('draws nothing while its page is shut', () => {
    install()
    mount()
    expect(host().children).toHaveLength(0)
  })

  it('draws a card for every agent and a link for every report once the page opens', async () => {
    install()
    mount()
    await openPage()
    expect(cards().map((c) => c.dataset.card).sort()).toEqual(['ana', 'coder', 'raven', 'writer'])
    expect(document.querySelectorAll('.canvas-edge')).toHaveLength(1)
    expect(card('raven').dataset.kind).toBe('lead')
    expect(card('coder').dataset.kind).toBe('worker')
    /* An agent with no row in the chart is drawn, and marked as not placed yet. */
    expect(card('writer').dataset.free).toBe('true')
    expect(card('ana').textContent).toContain('Analyst')
    expect(card('ana').textContent).toContain('m-1')
    expect(card('coder').getAttribute('aria-level')).toBe('2')
  })

  it('says so when there is nothing to draw', async () => {
    const h = install()
    h.source.load = async () => ({ agents: [], org: [], maxDepth: 6 })
    mount()
    await openPage()
    expect(document.querySelector('.canvas-empty')?.textContent).toBe('gui.canvas.empty')
  })

  it('shows an engine error in the bar', async () => {
    const h = install()
    h.source.load = async () => { throw new Error('engine down') }
    mount()
    await openPage()
    expect(document.querySelector('[role="alert"]')?.textContent).toBe('engine down')
  })

  it('closes with the page: the tree goes with it', async () => {
    install()
    mount()
    await openPage()
    await act(async () => { page.show(null) })
    expect(host().children).toHaveLength(0)
  })
})

describe('the keyboard', () => {
  it('walks the tree with the arrows, selecting as it goes', async () => {
    install()
    mount()
    await openPage()
    await press(card('raven'), 'ArrowDown')
    expect(store.get().selected).toEqual(['coder'])
    await press(card('coder'), 'ArrowUp')
    expect(store.get().selected).toEqual(['raven'])
  })

  it('opens the inspector on Enter and lets go on Escape', async () => {
    install()
    mount()
    await openPage()
    await press(card('ana'), 'Enter')
    expect(document.querySelector('aside[aria-label="gui.canvas.inspector"]')).toBeTruthy()
    expect(store.get().inspect).toBe('ana')
    await press(card('ana'), 'Escape')
    expect(store.get().inspect).toBeNull()
    expect(store.get().selected).toEqual([])
  })

  it('takes a card out of the chart on Delete, only after the reader confirms', async () => {
    const h = install()
    mount()
    await openPage()
    await press(card('coder'), 'Delete')
    expect(confirm.get()).toMatchObject({ open: true, body: 'gui.canvas.confirm_remove:{"name":"coder"}' })
    await act(async () => { confirm.answer(false) })
    expect(h.source.saveOrg).not.toHaveBeenCalled()
    await press(card('coder'), 'Delete')
    await act(async () => { confirm.answer(true) })
    await vi.waitFor(() => expect(h.source.saveOrg).toHaveBeenCalledTimes(1))
    expect(h.chart).toEqual([row('raven')])
  })

  it('does not ask about a card that is not in the chart', async () => {
    const h = install()
    mount()
    await openPage()
    await press(card('writer'), 'Delete')
    expect(confirm.get().open).toBe(false)
    expect(h.source.saveOrg).not.toHaveBeenCalled()
  })

  it('undoes the last change on Ctrl+Z', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { await store.reparent('writer', 'raven') })
    expect(h.chart).toHaveLength(3)
    await press(stage(), 'z', { ctrlKey: true })
    await vi.waitFor(() => expect(h.chart).toEqual([row('raven'), row('coder', 'raven')]))
  })

  it('zooms with + and - and fits with 0', async () => {
    install()
    mount()
    await openPage()
    const k0 = store.get().view.k
    await press(stage(), '+')
    expect(store.get().view.k).toBeGreaterThan(k0)
    await press(stage(), '-')
    expect(store.get().view.k).toBeCloseTo(k0, 5)
    await press(stage(), '0')
    expect(store.get().view.k).toBeGreaterThan(0)
  })
})

describe('the pointer', () => {
  it('selects a card on a click and lets go on a click in empty space', async () => {
    install()
    mount()
    await openPage()
    await act(async () => {
      fireEvent.pointerDown(card('coder'), { button: 0, clientX: 10, clientY: 10 })
      fireEvent.pointerUp(card('coder'), { button: 0, clientX: 10, clientY: 10 })
    })
    expect(store.get().selected).toEqual(['coder'])
    await act(async () => {
      fireEvent.pointerDown(stage(), { button: 0, clientX: 900, clientY: 900 })
      fireEvent.pointerUp(stage(), { button: 0, clientX: 900, clientY: 900 })
    })
    expect(store.get().selected).toEqual([])
  })

  it('opens the inspector on a double click', async () => {
    install()
    mount()
    await openPage()
    await act(async () => { fireEvent.doubleClick(card('raven')) })
    expect(store.get().inspect).toBe('raven')
  })

  it('picks a link by its wire and shows both ends', async () => {
    install()
    mount()
    await openPage()
    await act(async () => { fireEvent.click(document.querySelector('[data-wire="raven>coder"]')!) })
    expect(store.get().edge).toBe('raven>coder')
    expect(card('raven').dataset.hot).toBe('true')
    expect(card('coder').dataset.hot).toBe('true')
    expect(card('writer').dataset.hot).toBeUndefined()
  })
})

describe('the inspector', () => {
  it('changes the manager from the main tab', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { store.openInspector('writer') })
    const select = document.querySelector<HTMLSelectElement>('.canvas-field select')!
    expect([...select.options].map((o) => o.value)).toEqual(expect.arrayContaining(['', 'raven', 'coder']))
    await act(async () => { fireEvent.change(select, { target: { value: 'coder' } }) })
    await vi.waitFor(() => expect(h.chart).toContainEqual(row('writer', 'coder')))
  })

  it('says why a move is refused instead of sending it', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { await store.reparent('raven', 'coder') })
    expect(document.querySelector('[role="alert"]')?.textContent).toBe('gui.canvas.refuse_cycle')
    expect(h.source.saveOrg).not.toHaveBeenCalled()
  })

  it('edits and saves an employee\'s prompt, and offers the old text back', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { store.openInspector('ana', 'prompt') })
    await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('textarea.canvas-prompt')?.value).toBe('be kind'))
    const area = document.querySelector<HTMLTextAreaElement>('textarea.canvas-prompt')!
    const save = document.querySelector<HTMLButtonElement>('.canvas-save')!
    expect(save.disabled).toBe(true)
    await act(async () => { fireEvent.change(area, { target: { value: 'be brief' } }) })
    expect(save.disabled).toBe(false)
    await act(async () => { fireEvent.click(save) })
    await vi.waitFor(() => expect(h.prompts.get('ana')).toBe('be brief'))
    expect(document.querySelector('.canvas-hist')?.textContent).toContain('be kind')
  })

  it('has no prompt to offer for a roster agent', async () => {
    install()
    mount()
    await openPage()
    await act(async () => { store.openInspector('coder', 'prompt') })
    expect(document.querySelector('textarea.canvas-prompt')).toBeNull()
    expect(document.querySelector('.canvas-pane-body')?.textContent).toBe('gui.canvas.prompt_none')
  })

  it('lists the links of a card and unlinks a report', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { store.openInspector('raven', 'links') })
    const list = document.querySelector('.canvas-hist')!
    expect(list.textContent).toContain('coder')
    await act(async () => { fireEvent.click([...list.querySelectorAll('button')].find((b) => b.textContent === 'gui.canvas.unlink')!) })
    await vi.waitFor(() => expect(h.chart).toEqual([row('raven'), row('coder', null)]))
  })
})

describe('the file panel', () => {
  it('shows the chart as text and applies an edited one', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { fireEvent.click(tool('gui.canvas.file')) })
    const area = document.querySelector<HTMLTextAreaElement>('.canvas-json')!
    const file = JSON.parse(area.value) as { org: OrgRow[] }
    expect(file.org.map((r) => r.name)).toEqual(['raven', 'coder'])
    file.org.push(row('writer', 'raven'))
    await act(async () => { fireEvent.change(area, { target: { value: JSON.stringify(file) } }) })
    await act(async () => { fireEvent.click(tool('gui.canvas.file_apply')) })
    await vi.waitFor(() => expect(h.chart).toContainEqual(row('writer', 'raven')))
    await vi.waitFor(() => expect(document.querySelector('.canvas-json')).toBeNull())
  })

  it('keeps the panel open and says so when the text is not a chart', async () => {
    const h = install()
    mount()
    await openPage()
    await act(async () => { fireEvent.click(tool('gui.canvas.file')) })
    await act(async () => { fireEvent.change(document.querySelector('.canvas-json')!, { target: { value: 'not json' } }) })
    await act(async () => { fireEvent.click(tool('gui.canvas.file_apply')) })
    expect(document.querySelector('.canvas-json')).toBeTruthy()
    expect(document.querySelector('[role="alert"]')?.textContent).toBe('gui.canvas.import_bad')
    expect(h.source.saveOrg).not.toHaveBeenCalled()
  })
})
