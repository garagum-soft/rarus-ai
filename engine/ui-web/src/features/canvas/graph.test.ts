import { describe, expect, it } from 'vitest'

import {
  CARD_H, CARD_W, MAX_DEPTH, branchOf, cardAt, cardsIn, chain, dropFromChart, editRow, edgesOf, exportGraph, fitView,
  heightOf, importGraph, layoutTree, levelOf, managersFor, mergeAgents, reportsOf, setParent, step, treeOrder, wirePath, zoomAt,
} from './graph'

import type { AgentNode, OrgRow } from './graph'

const r = (name: string, reports_to: string | null = null): OrgRow => ({ name, reports_to, department: '', title: '' })
const node = (name: string): AgentNode => ({ name, origin: 'agent', role: '', description: '', model: '', ready: true })
const known = ['lead', 'dev', 'qa', 'ops', 'free']

const chart = [r('lead'), r('dev', 'lead'), r('qa', 'dev'), r('ops', 'lead')]

describe('reading the chart', () => {
  it('names the managers above a name, nearest first', () => {
    expect(chain(chart, 'qa')).toEqual(['dev', 'lead'])
    expect(chain(chart, 'lead')).toEqual([])
  })

  it('counts levels from a head, and zero for a name outside the chart', () => {
    expect(levelOf(chart, 'lead')).toBe(1)
    expect(levelOf(chart, 'qa')).toBe(3)
    expect(levelOf(chart, 'free')).toBe(0)
  })

  it('lists direct reports by name and the height of a branch', () => {
    expect(reportsOf(chart, 'lead')).toEqual(['dev', 'ops'])
    expect(heightOf(chart, 'lead')).toBe(3)
    expect(heightOf(chart, 'ops')).toBe(1)
  })

  it('draws one link from each manager to each report', () => {
    expect(edgesOf(chart)).toEqual([
      { from: 'lead', to: 'dev' },
      { from: 'dev', to: 'qa' },
      { from: 'lead', to: 'ops' },
    ])
  })

  it('keeps a name\'s whole line lit: above it and below it, not its side branches', () => {
    expect([...branchOf(chart, 'dev')].sort()).toEqual(['dev', 'lead', 'qa'])
  })

  it('does not follow a loop forever', () => {
    expect(chain([r('a', 'b'), r('b', 'a')], 'a').length).toBeLessThan(3)
  })
})

describe('moving a card onto another', () => {
  it('makes it report there, and puts both in the chart when they were free', () => {
    const move = setParent([], 'dev', 'lead', known)
    expect(move).toEqual({ ok: true, org: [r('lead'), r('dev', 'lead')] })
  })

  it('moves a branch whole', () => {
    const move = setParent(chart, 'dev', 'ops', known)
    expect(move.ok && chain(move.org, 'qa')).toEqual(['dev', 'ops', 'lead'])
  })

  it('lets a card report to nobody', () => {
    const move = setParent(chart, 'dev', null, known)
    expect(move.ok && move.org.find((x) => x.name === 'dev')?.reports_to).toBeNull()
  })

  it('refuses itself, a loop, an unknown name', () => {
    expect(setParent(chart, 'dev', 'dev', known)).toEqual({ ok: false, reason: 'self' })
    expect(setParent(chart, 'lead', 'qa', known)).toEqual({ ok: false, reason: 'cycle' })
    expect(setParent(chart, 'dev', 'ghost', known)).toEqual({ ok: false, reason: 'unknown' })
    expect(setParent(chart, 'ghost', 'dev', known)).toEqual({ ok: false, reason: 'unknown' })
  })

  it('refuses a move that would push a branch past six levels', () => {
    const names = ['n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'extra']
    const deep = names.slice(0, 6).map((n, i) => r(n, i ? names[i - 1] as string : null))
    expect(MAX_DEPTH).toBe(6)
    expect(setParent(deep, 'extra', 'n6', names)).toEqual({ ok: false, reason: 'depth' })
    expect(setParent(deep, 'extra', 'n5', names).ok).toBe(true)
  })

  it('counts the whole branch being moved, not only the card', () => {
    const names = ['a', 'b', 'c', 'd', 'e', 'x', 'y', 'z']
    const org = [r('a'), r('b', 'a'), r('c', 'b'), r('d', 'c'), r('e', 'd'), r('x'), r('y', 'x'), r('z', 'y')]
    // x-y-z is three high; under e (level 5) it would reach level 8.
    expect(setParent(org, 'x', 'e', names)).toEqual({ ok: false, reason: 'depth' })
    expect(setParent(org, 'x', 'c', names)).toEqual({ ok: true, org: expect.any(Array) })
  })

  it('offers as managers only those a move to is allowed for', () => {
    expect(managersFor(chart, 'dev', known)).toEqual(['free', 'lead', 'ops'])
  })

  it('does not change the rows it was given', () => {
    const before = JSON.stringify(chart)
    setParent(chart, 'dev', 'ops', known)
    dropFromChart(chart, 'dev')
    editRow(chart, 'dev', { title: 'x' })
    expect(JSON.stringify(chart)).toBe(before)
  })

  it('takes a name out of the chart and hands its reports to its manager', () => {
    const next = dropFromChart(chart, 'dev')
    expect(next.map((x) => x.name)).toEqual(['lead', 'qa', 'ops'])
    expect(next.find((x) => x.name === 'qa')?.reports_to).toBe('lead')
  })

  it('edits a row, or adds one for a free card', () => {
    expect(editRow(chart, 'dev', { title: 'Dev' }).find((x) => x.name === 'dev')?.title).toBe('Dev')
    expect(editRow(chart, 'free', { department: 'R&D' }).find((x) => x.name === 'free')).toEqual({
      name: 'free', reports_to: null, department: 'R&D', title: '',
    })
  })
})

describe('the cards', () => {
  it('merges employees, roster agents and chart-only names into one card per name', () => {
    const nodes = mergeAgents(
      [{ name: 'Raven-Code', description: 'code', enabled: true, configured: true }, { name: 'dev', enabled: false }],
      [{ name: 'dev', role: 'writes code', owns: 'src', model: 'rarus/auto' }],
      [r('ghost')],
    )
    expect(nodes.map((n) => [n.name, n.origin])).toEqual([['dev', 'employee'], ['ghost', 'chart'], ['Raven-Code', 'agent']])
    const dev = nodes.find((n) => n.name === 'dev') as AgentNode
    expect(dev).toMatchObject({ role: 'writes code', description: 'src', model: 'rarus/auto', ready: false })
    expect(nodes.find((n) => n.name === 'ghost')?.ready).toBe(false)
  })
})

describe('the layout', () => {
  const nodes = known.map(node)

  it('puts a head above its reports, which sit side by side without touching', () => {
    const pos = layoutTree(nodes, chart)
    const lead = pos.get('lead')!
    const dev = pos.get('dev')!
    const ops = pos.get('ops')!
    expect(dev.y).toBeGreaterThan(lead.y + CARD_H)
    expect(ops.y).toBe(dev.y)
    expect(ops.x - dev.x).toBeGreaterThanOrEqual(CARD_W)
    expect(lead.x).toBeCloseTo((dev.x + ops.x) / 2, 5)
  })

  it('puts free cards in a row below the chart', () => {
    const pos = layoutTree(nodes, chart)
    expect(pos.get('free')!.y).toBeGreaterThan(pos.get('qa')!.y)
  })

  it('gives every card a place and none overlaps another', () => {
    const pos = layoutTree(nodes, chart)
    expect(pos.size).toBe(nodes.length)
    const all = [...pos.values()]
    for (const [i, a] of all.entries()) {
      for (const b of all.slice(i + 1)) {
        expect(a.x < b.x + CARD_W && b.x < a.x + CARD_W && a.y < b.y + CARD_H && b.y < a.y + CARD_H).toBe(false)
      }
    }
  })

  it('lays a flat roster out as one row', () => {
    const pos = layoutTree(nodes, [])
    expect(new Set([...pos.values()].map((p) => p.y)).size).toBe(1)
  })
})

describe('the view', () => {
  it('fits every card inside the box', () => {
    const pos = layoutTree(known.map(node), chart)
    const view = fitView(pos, 800, 500)
    for (const p of pos.values()) {
      expect(p.x * view.k + view.x).toBeGreaterThanOrEqual(0)
      expect((p.x + CARD_W) * view.k + view.x).toBeLessThanOrEqual(800)
      expect(p.y * view.k + view.y).toBeGreaterThanOrEqual(0)
      expect((p.y + CARD_H) * view.k + view.y).toBeLessThanOrEqual(500)
    }
  })

  it('keeps the spot under the pointer still while zooming, within the limits', () => {
    const view = zoomAt({ x: 10, y: 20, k: 1 }, 2, 100, 100)
    expect((100 - view.x) / view.k).toBeCloseTo(90, 5)
    expect((100 - view.y) / view.k).toBeCloseTo(80, 5)
    expect(zoomAt({ x: 0, y: 0, k: 1 }, 1000, 0, 0).k).toBeLessThanOrEqual(2.5)
    expect(zoomAt({ x: 0, y: 0, k: 1 }, 0.0001, 0, 0).k).toBeGreaterThanOrEqual(0.2)
  })

  it('finds the card under a point and the cards inside a rectangle', () => {
    const pos = new Map([['a', { x: 0, y: 0 }], ['b', { x: 400, y: 0 }]])
    expect(cardAt(pos, 10, 10)).toBe('a')
    expect(cardAt(pos, 10, 10, 'a')).toBeNull()
    expect(cardAt(pos, 300, 10)).toBeNull()
    expect(cardsIn(pos, -5, -5, 20, 20)).toEqual(['a'])
    expect(cardsIn(pos, 500, 100, -10, -10).sort()).toEqual(['a', 'b'])
  })

  it('draws a link from the bottom of the manager to the top of the report', () => {
    expect(wirePath({ x: 0, y: 0 }, { x: 0, y: 300 })).toBe(`M${CARD_W / 2} ${CARD_H} C${CARD_W / 2} ${CARD_H + 91} ${CARD_W / 2} ${300 - 91} ${CARD_W / 2} 300`)
  })
})

describe('the keyboard', () => {
  const nodes = known.map(node)
  const order = treeOrder(nodes, chart)

  it('walks the tree in order, free cards last', () => {
    expect(order).toEqual(['lead', 'dev', 'qa', 'ops', 'free'])
  })

  it('goes up to the manager, down to the first report and across the siblings', () => {
    expect(step(chart, 'qa', 'ArrowUp', order)).toBe('dev')
    expect(step(chart, 'lead', 'ArrowDown', order)).toBe('dev')
    expect(step(chart, 'dev', 'ArrowRight', order)).toBe('ops')
    expect(step(chart, 'ops', 'ArrowLeft', order)).toBe('dev')
  })

  it('stays put at an end', () => {
    expect(step(chart, 'lead', 'ArrowUp', order)).toBe('lead')
    expect(step(chart, 'qa', 'ArrowDown', order)).toBe('qa')
    expect(step(chart, 'ops', 'ArrowRight', order)).toBe('ops')
  })
})

describe('the file', () => {
  it('comes back as it went out', () => {
    const rows: OrgRow[] = [{ name: 'lead', reports_to: null, department: 'Core', title: 'Head' }, r('dev', 'lead')]
    const positions = new Map([['lead', { x: 1, y: 2 }], ['dev', { x: 3.5, y: 4 }]])
    const back = importGraph(exportGraph(rows, positions))
    expect(back).toEqual({ org: rows, positions })
  })

  it('refuses what this page did not write', () => {
    expect(importGraph('not json')).toBeNull()
    expect(importGraph('{"version":2,"org":[]}')).toBeNull()
    expect(importGraph('{"version":1,"org":[{"name":""}]}')).toBeNull()
    expect(importGraph('{"version":1}')).toBeNull()
  })

  it('drops positions that are not numbers', () => {
    const back = importGraph('{"version":1,"org":[],"positions":{"a":{"x":"1","y":2},"b":{"x":1,"y":2}}}')
    expect([...(back?.positions.keys() ?? [])]).toEqual(['b'])
  })
})
