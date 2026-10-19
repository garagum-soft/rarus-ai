// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as page from '../../state/page'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { AgentNode, OrgRow } from './graph'
import type { Company } from './org'
import type { CanvasSource } from './types'

const node = (name: string, origin: AgentNode['origin'] = 'agent'): AgentNode => ({
  name, origin, role: '', description: '', model: '', ready: true,
})
const row = (name: string, reports_to: string | null = null, department = '', title = ''): OrgRow => ({
  name, reports_to, department, title, company: '', avatar: '',
})

interface Harness {
  source: CanvasSource
  saved: OrgRow[][]
  prompts: Map<string, string>
  failNext: string | null
  chart: OrgRow[]
  companies: Company[]
}

function install(chart: OrgRow[], agents: AgentNode[] = [node('raven'), node('coder'), node('writer'), node('ana', 'employee')]): Harness {
  const h: Harness = { source: null as unknown as CanvasSource, saved: [], prompts: new Map([['ana', 'be kind']]), failNext: null, chart, companies: [] }
  h.source = {
    load: vi.fn(async () => ({ agents, org: h.chart.map((r) => ({ ...r })), companies: [], maxDepth: 6 })),
    saveOrg: vi.fn(async (rows: OrgRow[], companies: Company[] = []) => {
      if (h.failNext) {
        const message = h.failNext
        h.failNext = null
        throw new Error(message)
      }
      h.saved.push(rows.map((r) => ({ ...r })))
      h.chart = rows.map((r) => ({ ...r }))
      h.companies = companies.map((c) => ({ ...c }))
      return { org: h.chart.map((r) => ({ ...r })), companies: h.companies.map((c) => ({ ...c })) }
    }),
    promptGet: vi.fn(async (name: string) => h.prompts.get(name) ?? ''),
    promptSet: vi.fn(async (name: string, text: string) => {
      h.prompts.set(name, text)
      return true
    }),
    hire: vi.fn(async (spec) => ({ name: spec.name, origin: 'employee' as const, role: spec.role, description: '', model: '', ready: true })),
    retire: vi.fn(async () => undefined),
  }
  setSources({ canvas: h.source })
  return h
}

beforeEach(() => {
  localStorage.clear()
  document.body.innerHTML =
    '<div class="app"></div>' +
    ['extAgentsPage', 'connectionsPage', 'knowledgePage', 'canvasPage']
      .map((id) => `<section class="page" id="${id}" data-open="false"><div class="work"></div></section>`).join('') +
    '<aside class="detail" id="detail" data-open="false"></aside>'
})

afterEach(() => {
  page.show(null)
  page._resetForTests()
  store._resetForTests()
  resetSources()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('loading', () => {
  it('opens the page and reads the chart', async () => {
    install([row('coder', 'raven')])
    store.open()
    expect(page.get()).toBe('canvasPage')
    await vi.waitFor(() => expect(store.get().loaded).toBe(true))
    expect(store.names()).toEqual(['raven', 'coder', 'writer', 'ana'])
    expect(store.get().org).toEqual([row('coder', 'raven')])
    expect(store.get().maxDepth).toBe(6)
  })

  it('says what went wrong when the chart cannot be read', async () => {
    const h = install([])
    ;(h.source.load as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('engine down'))
    await store.load()
    expect(store.get()).toMatchObject({ loaded: true, loading: false, error: 'engine down' })
  })

  it('keeps the places the reader chose for cards that still exist, and forgets the rest', async () => {
    localStorage.setItem('rarus.canvas.positions', JSON.stringify({ coder: { x: 5, y: 6 }, gone: { x: 1, y: 1 } }))
    install([row('coder', 'raven')])
    await store.load()
    expect(store.get().manual).toEqual({ coder: { x: 5, y: 6 } })
    expect(store.positions().get('coder')).toEqual({ x: 5, y: 6 })
  })
})

describe('moving a card under a manager', () => {
  it('sends the whole chart and keeps the engine\'s answer', async () => {
    const h = install([row('raven'), row('coder', 'raven')])
    await store.load()
    expect(await store.reparent('writer', 'coder')).toBe(true)
    expect(h.saved).toHaveLength(1)
    expect(h.saved[0]).toEqual([row('raven'), row('coder', 'raven'), row('writer', 'coder')])
    expect(store.get().org).toEqual(h.saved[0])
    expect(store.get().undo).toHaveLength(1)
  })

  it('refuses a loop, a self-report and a missing name without asking the engine', async () => {
    const h = install([row('raven'), row('coder', 'raven')])
    await store.load()
    expect(await store.reparent('raven', 'coder')).toBe(false)
    expect(store.get().refused).toBe('cycle')
    expect(await store.reparent('coder', 'coder')).toBe(false)
    expect(store.get().refused).toBe('self')
    expect(await store.reparent('coder', 'nobody')).toBe(false)
    expect(store.get().refused).toBe('unknown')
    expect(h.source.saveOrg).not.toHaveBeenCalled()
    expect(store.get().org).toEqual([row('raven'), row('coder', 'raven')])
  })

  it('refuses a chart deeper than six levels', async () => {
    const names = ['a', 'b', 'c', 'd', 'e', 'f', 'g']
    const agents = names.map((n) => node(n))
    install(names.slice(0, 6).map((n, i) => row(n, i ? names[i - 1] as string : null)), agents)
    await store.load()
    expect(await store.reparent('g', 'f')).toBe(false)
    expect(store.get().refused).toBe('depth')
  })

  it('puts the old chart back when the engine says no, and shows its reason', async () => {
    const h = install([row('raven')])
    await store.load()
    h.failNext = 'org: cycle'
    expect(await store.reparent('coder', 'raven')).toBe(false)
    expect(store.get().org).toEqual([row('raven')])
    expect(store.get().error).toBe('org: cycle')
    expect(store.get().undo).toEqual([])
  })

  it('takes a link away: the report then reports to nobody', async () => {
    const h = install([row('raven'), row('coder', 'raven')])
    await store.load()
    store.selectEdge('raven>coder')
    expect(store.get().selected).toEqual(['raven', 'coder'])
    expect(await store.unlinkSelected()).toBe(true)
    expect(h.saved[0]).toEqual([row('raven'), row('coder', null)])
    expect(store.get().edge).toBeNull()
  })

  it('takes a card out of the chart and lifts its reports to its manager', async () => {
    const h = install([row('raven'), row('coder', 'raven'), row('writer', 'coder')])
    await store.load()
    expect(await store.removeFromChart('coder')).toBe(true)
    expect(h.saved[0]).toEqual([row('raven'), row('writer', 'raven')])
    expect(await store.removeFromChart('coder')).toBe(false)
  })

  it('edits the department and the title on the card\'s row', async () => {
    const h = install([row('raven')])
    await store.load()
    await store.editMeta('raven', { department: 'Ops', title: 'Head' })
    expect(h.saved[0]).toEqual([row('raven', null, 'Ops', 'Head')])
  })
})

describe('undo', () => {
  it('walks the chart back one change at a time and tells the engine', async () => {
    const h = install([row('raven')])
    await store.load()
    await store.reparent('coder', 'raven')
    await store.reparent('writer', 'raven')
    expect(store.get().undo).toHaveLength(2)
    expect(await store.undo()).toBe(true)
    expect(store.get().org).toEqual([row('raven'), row('coder', 'raven')])
    expect(h.chart).toEqual([row('raven'), row('coder', 'raven')])
    expect(await store.undo()).toBe(true)
    expect(store.get().org).toEqual([row('raven')])
    expect(await store.undo()).toBe(false)
  })

  it('keeps the present chart when the engine refuses the older one', async () => {
    const h = install([row('raven')])
    await store.load()
    await store.reparent('coder', 'raven')
    h.failNext = 'busy'
    expect(await store.undo()).toBe(false)
    expect(store.get().org).toEqual([row('raven'), row('coder', 'raven')])
    expect(store.get().undo).toHaveLength(1)
    expect(store.get().error).toBe('busy')
  })

  it('remembers only the last forty changes', async () => {
    install([row('raven')])
    await store.load()
    for (let i = 0; i < 45; i++) await store.editMeta('raven', { title: `t${i}` })
    expect(store.get().undo).toHaveLength(40)
  })
})

describe('the file', () => {
  it('round-trips the chart and the places cards were put', async () => {
    const h = install([row('raven'), row('coder', 'raven')])
    await store.load()
    store.placeCard('coder', { x: 10, y: 20 })
    const text = store.exportText()
    await store.reparent('coder', null)
    expect(await store.importText(text)).toBe(true)
    expect(h.chart).toEqual([row('raven'), row('coder', 'raven')])
    /* The file carries every card's place, so the layout comes back exactly as it was exported. */
    expect(store.get().manual).toMatchObject({ coder: { x: 10, y: 20 }, raven: { x: 0, y: 0 } })
  })

  it('refuses text that is not a chart', async () => {
    const h = install([row('raven')])
    await store.load()
    expect(await store.importText('{ nope')).toBe(false)
    expect(store.get().error).toBe('import')
    expect(h.source.saveOrg).not.toHaveBeenCalled()
  })
})

describe('selection and the view', () => {
  it('selects one card, adds to the selection, and lights the branch', async () => {
    install([row('raven'), row('coder', 'raven'), row('writer', 'coder')])
    await store.load()
    expect(store.lit()).toBeNull()
    store.select('coder')
    expect(store.get().selected).toEqual(['coder'])
    expect([...store.lit()!].sort()).toEqual(['coder', 'raven', 'writer'])
    store.select('ana', true)
    expect(store.get().selected).toEqual(['coder', 'ana'])
    store.select('ana', true)
    expect(store.get().selected).toEqual(['coder'])
    store.clearSelection()
    expect(store.get().selected).toEqual([])
  })

  it('picks the cards inside a box', async () => {
    install([row('raven'), row('coder', 'raven')])
    await store.load()
    const pos = store.positions()
    const p = pos.get('raven')!
    store.selectInBox(p.x - 1, p.y - 1, p.x + 5, p.y + 5)
    expect(store.get().selected).toEqual(['raven'])
  })

  it('zooms inside its limits and fits the cards in the box', async () => {
    install([row('raven'), row('coder', 'raven')])
    await store.load()
    for (let i = 0; i < 40; i++) store.zoomBy(1.5)
    expect(store.get().view.k).toBeLessThanOrEqual(2.5)
    store.setSize(800, 600)
    store.fit()
    const { k } = store.get().view
    expect(k).toBeGreaterThan(0)
    expect(k).toBeLessThanOrEqual(2.5)
  })

  it('lets a card go back to the tree\'s place for it', async () => {
    install([row('raven'), row('coder', 'raven')])
    await store.load()
    const was = store.positions().get('coder')
    store.placeCard('coder', { x: 999, y: 999 })
    expect(store.positions().get('coder')).toEqual({ x: 999, y: 999 })
    store.unplace('coder')
    expect(store.positions().get('coder')).toEqual(was)
    store.placeCard('coder', { x: 1, y: 1 })
    store.autoLayout()
    expect(store.get().manual).toEqual({})
  })
})

describe('the prompt', () => {
  it('reads an employee\'s prompt and saves a change, keeping the old text in the history', async () => {
    const h = install([row('ana')])
    await store.load()
    store.openInspector('ana', 'prompt')
    await vi.waitFor(() => expect(store.get().prompt.loading).toBe(false))
    expect(store.get().prompt).toMatchObject({ name: 'ana', text: 'be kind', saved: 'be kind' })
    expect(await store.savePrompt()).toBe(false)
    store.editPrompt('be brief')
    expect(await store.savePrompt()).toBe(true)
    expect(h.prompts.get('ana')).toBe('be brief')
    expect(store.get().prompt.history).toEqual(['be kind'])
    expect(store.historyOf('ana')).toEqual(['be kind'])
    store.rollbackTo(0)
    expect(store.get().prompt.text).toBe('be kind')
  })

  it('leaves a failed save as a draft and says why', async () => {
    const h = install([row('ana')])
    await store.load()
    store.openInspector('ana', 'prompt')
    await vi.waitFor(() => expect(store.get().prompt.loading).toBe(false))
    ;(h.source.promptSet as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('no disk'))
    store.editPrompt('x')
    expect(await store.savePrompt()).toBe(false)
    expect(store.get().error).toBe('no disk')
    expect(store.get().prompt.saved).toBe('be kind')
    expect(store.historyOf('ana')).toEqual([])
  })

  it('has no prompt to edit for an agent that is not an employee', async () => {
    const h = install([row('raven')])
    await store.load()
    store.openInspector('raven', 'prompt')
    await Promise.resolve()
    expect(store.isEmployee('raven')).toBe(false)
    expect(h.source.promptGet).not.toHaveBeenCalled()
    expect(store.get().prompt.name).toBe('')
  })
})

describe('companies and departments', () => {
  it('adds a company where it was asked for and saves it with the chart', async () => {
    const h = install([row('raven')])
    await store.load()
    const id = await store.addCompany('Acme', '', { x: 900, y: 40 })
    expect(id).toBe('acme')
    expect(h.companies.map((c) => c.name)).toEqual(['Acme'])
    expect(store.layout().boxes.companies.get('acme')).toMatchObject({ x: 900, y: 40 })
  })

  it('adds departments, refuses a second one of the same name, and renames one with its people', async () => {
    const h = install([row('raven')])
    await store.load()
    await store.addCompany('Acme', '')
    expect(await store.addDepartment('acme', 'Dev')).toBe(true)
    expect(await store.addDepartment('acme', 'Dev')).toBe(false)
    expect(store.get().error).toBe('dept_taken')
    await store.moveMember('raven', 'acme', 'Dev')
    await store.updateDepartment('acme', 'Dev', { name: 'Engineering', background: 'night' })
    expect(h.companies[0]?.departments[0]).toMatchObject({ name: 'Engineering', background: 'night' })
    expect(h.chart.find((r) => r.name === 'raven')?.department).toBe('Engineering')
  })

  it('writes the reporting lines from the boxes once someone heads them', async () => {
    const h = install([])
    await store.load()
    await store.addCompany('Acme', '')
    await store.addDepartment('acme', 'Dev')
    await store.moveMember('raven', 'acme', '')
    await store.moveMember('coder', 'acme', 'Dev')
    await store.moveMember('writer', 'acme', 'Dev')
    await store.makeHead('raven')
    await store.makeHead('coder')
    const lines = Object.fromEntries(h.chart.map((r) => [r.name, r.reports_to]))
    expect(lines).toEqual({ raven: null, coder: 'raven', writer: 'coder' })
  })

  it('hires an employee straight into a department with an avatar', async () => {
    const h = install([])
    await store.load()
    await store.addCompany('Acme', '')
    await store.addDepartment('acme', 'Dev')
    expect(await store.hire({ name: 'Zara', role: 'Tester', system_prompt: 'You test.' }, 'acme', 'Dev', 'r2')).toBe(true)
    expect(h.source.hire).toHaveBeenCalledWith({ name: 'Zara', role: 'Tester', system_prompt: 'You test.' })
    expect(h.chart.find((r) => r.name === 'Zara')).toMatchObject({ company: 'acme', department: 'Dev', avatar: 'r2' })
    expect(store.isEmployee('Zara')).toBe(true)
  })

  it('says why a hire was refused and changes nothing', async () => {
    const h = install([])
    await store.load()
    ;(h.source.hire as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('an employee name needs a latin letter'))
    expect(await store.hire({ name: 'Анна', role: 'x', system_prompt: 'y' }, '', '', 'p1')).toBe(false)
    expect(store.get().error).toBe('an employee name needs a latin letter')
    expect(h.saved).toEqual([])
  })

  it('lets an employee go: its card and row leave, its head post is left empty', async () => {
    const h = install([], [node('ana', 'employee'), node('raven')])
    await store.load()
    await store.addCompany('Acme', '')
    await store.moveMember('ana', 'acme', '')
    await store.makeHead('ana')
    expect(await store.retire('ana')).toBe(true)
    expect(h.source.retire).toHaveBeenCalledWith('ana')
    expect(store.names()).toEqual(['raven'])
    expect(h.chart).toEqual([])
    expect(h.companies[0]?.head).toBe('')
  })

  it('freezes and unfreezes a company, and deleting it leaves its people on the canvas', async () => {
    const h = install([])
    await store.load()
    await store.addCompany('Acme', '')
    await store.moveMember('coder', 'acme', '')
    await store.updateCompany('acme', { frozen: true })
    expect(h.companies[0]?.frozen).toBe(true)
    await store.deleteCompany('acme')
    expect(h.companies).toEqual([])
    expect(h.chart.find((r) => r.name === 'coder')).toMatchObject({ company: '', department: '' })
  })

  it('takes everything back when the engine refuses a change', async () => {
    const h = install([])
    await store.load()
    await store.addCompany('Acme', '')
    h.failNext = 'the head of Acme must work in it'
    expect(await store.moveMember('coder', 'acme', '')).toBe(false)
    expect(store.get().error).toBe('the head of Acme must work in it')
    expect(store.rowOf('coder')).toBeUndefined()
  })

  it('moves a whole box with what is in it', async () => {
    install([])
    await store.load()
    await store.addCompany('Acme', '', { x: 0, y: 0 })
    await store.moveMember('coder', 'acme', '')
    const before = store.positions().get('coder')!
    const grip = store.gripBox({ kind: 'company', id: 'acme' })
    store.moveBox(grip, 100, 50, true)
    expect(store.positions().get('coder')).toEqual({ x: before.x + 100, y: before.y + 50 })
    expect(store.layout().boxes.companies.get('acme')).toMatchObject({ x: 100, y: 50 })
  })

  it('exports a company and brings it back as a new one', async () => {
    const h = install([])
    await store.load()
    await store.addCompany('Acme', '')
    await store.addDepartment('acme', 'Dev')
    await store.moveMember('coder', 'acme', 'Dev')
    const text = store.exportCompanyText('acme')
    expect(await store.importText(text)).toBe(true)
    expect(h.companies.map((c) => c.id)).toEqual(['acme', 'acme-2'])
    expect(h.chart.find((r) => r.name === 'coder')).toMatchObject({ company: 'acme-2', department: 'Dev' })
  })
})
