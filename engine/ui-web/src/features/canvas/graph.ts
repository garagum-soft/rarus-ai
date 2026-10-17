/* The agent hierarchy as plain data: who reports to whom, where each card sits,
 * which moves are allowed. No DOM and no gateway -- the store and the page read
 * these, and the tests drive them directly.
 *
 * The chart is `subagents.org` (name, reports_to, department, title). A roster
 * agent that has no row is "free": drawn, but not in the chart yet. The engine
 * refuses a chart that is not a forest or is deeper than `MAX_DEPTH`
 * (raven/agent/subagent/org_store.py); `setParent` refuses the same moves up front so
 * the page never sends one. */

export interface OrgRow {
  name: string
  reports_to: string | null
  department: string
  title: string
}

/** Where a card's data came from: a hired employee, a roster agent, or only the chart. */
export type Origin = 'employee' | 'agent' | 'chart'

export interface AgentNode {
  name: string
  origin: Origin
  role: string
  description: string
  model: string
  /* A roster agent that cannot be dispatched now (off, or not set up). */
  ready: boolean
}

export interface Pos {
  x: number
  y: number
}

export interface Edge {
  from: string
  to: string
}

export const MAX_DEPTH = 6
export const CARD_W = 232
export const CARD_H = 118
const GAP_X = 28
const GAP_Y = 72

export type Refusal = 'self' | 'cycle' | 'depth' | 'unknown'

export type Move = { ok: true; org: OrgRow[] } | { ok: false; reason: Refusal }

const row = (name: string, reports_to: string | null = null): OrgRow => ({ name, reports_to, department: '', title: '' })

export const byName = (org: readonly OrgRow[]): Map<string, OrgRow> => new Map(org.map((r) => [r.name, r]))

/** The managers above a name, nearest first. Stops on a loop instead of following it. */
export function chain(org: readonly OrgRow[], name: string): string[] {
  const rows = byName(org)
  const out: string[] = []
  let at = rows.get(name)?.reports_to ?? null
  while (at && !out.includes(at) && at !== name) {
    out.push(at)
    at = rows.get(at)?.reports_to ?? null
  }
  return out
}

/** Levels from a head down to this name; a head is level 1, a name outside the chart level 0. */
export function levelOf(org: readonly OrgRow[], name: string): number {
  return byName(org).has(name) ? chain(org, name).length + 1 : 0
}

export function reportsOf(org: readonly OrgRow[], name: string): string[] {
  return org.filter((r) => r.reports_to === name).map((r) => r.name).sort(compare)
}

/** The deepest level at or below a name, counting the name as 1. */
export function heightOf(org: readonly OrgRow[], name: string): number {
  const below = reportsOf(org, name)
  return below.length ? 1 + Math.max(...below.map((child) => heightOf(org, child))) : 1
}

export const edgesOf = (org: readonly OrgRow[]): Edge[] =>
  org.filter((r) => r.reports_to).map((r) => ({ from: r.reports_to as string, to: r.name }))

const compare = (a: string, b: string): number => a.localeCompare(b)

/* A name's whole branch: itself, everyone above it and everyone below it. What stays lit
   when a card is picked; the rest dims. */
export function branchOf(org: readonly OrgRow[], name: string): Set<string> {
  const out = new Set<string>([name, ...chain(org, name)])
  const stack = reportsOf(org, name)
  while (stack.length) {
    const next = stack.pop() as string
    if (out.has(next)) continue
    out.add(next)
    stack.push(...reportsOf(org, next))
  }
  return out
}

/**
 * Who `child` may report to: not itself, not anyone below it, and nobody whose line would
 * push `child`'s branch past `MAX_DEPTH`.
 */
export function managersFor(org: readonly OrgRow[], child: string, known: readonly string[]): string[] {
  return known.filter((name) => name !== child && check(org, child, name) === null).sort(compare)
}

function check(org: readonly OrgRow[], child: string, parent: string | null): Refusal | null {
  if (parent === null) return null
  if (parent === child) return 'self'
  if (chain(org, parent).includes(child)) return 'cycle'
  const depth = levelOf(org, parent) + heightOf(org, child)
  return depth > MAX_DEPTH ? 'depth' : null
}

/**
 * The chart after `child` reports to `parent` (null: to nobody), or why not. Either name that
 * has no row yet gets one, so dropping a free card on another places both in the chart.
 */
export function setParent(org: readonly OrgRow[], child: string, parent: string | null, known: readonly string[]): Move {
  if (!known.includes(child) || (parent !== null && !known.includes(parent))) return { ok: false, reason: 'unknown' }
  const refusal = check(org, child, parent)
  if (refusal) return { ok: false, reason: refusal }
  const next = org.map((r) => ({ ...r }))
  const add = (name: string): OrgRow => {
    let found = next.find((r) => r.name === name)
    if (!found) {
      found = row(name)
      next.push(found)
    }
    return found
  }
  if (parent !== null) add(parent)
  add(child).reports_to = parent
  return { ok: true, org: next }
}

/** Rows with `name` removed from the chart; its reports move up to its manager. */
export function dropFromChart(org: readonly OrgRow[], name: string): OrgRow[] {
  const manager = byName(org).get(name)?.reports_to ?? null
  return org.filter((r) => r.name !== name).map((r) => (r.reports_to === name ? { ...r, reports_to: manager } : { ...r }))
}

export function editRow(org: readonly OrgRow[], name: string, patch: Partial<Pick<OrgRow, 'department' | 'title'>>): OrgRow[] {
  const next = org.map((r) => (r.name === name ? { ...r, ...patch } : { ...r }))
  return next.some((r) => r.name === name) ? next : [...next, { ...row(name), ...patch }]
}

/* -- the nodes ------------------------------------------------------------- */

interface RosterLike { name: string; description?: string; enabled?: boolean; configured?: boolean }
interface EmployeeLike { name: string; role?: string; owns?: string; model?: string }

/** Employees, roster agents and chart-only names as one list, one card per name. */
export function mergeAgents(roster: readonly RosterLike[], employees: readonly EmployeeLike[], org: readonly OrgRow[]): AgentNode[] {
  const nodes = new Map<string, AgentNode>()
  for (const r of roster) {
    nodes.set(r.name, {
      name: r.name,
      origin: 'agent',
      role: '',
      description: r.description ?? '',
      model: '',
      ready: r.enabled !== false && r.configured !== false,
    })
  }
  for (const e of employees) {
    const was = nodes.get(e.name)
    nodes.set(e.name, {
      name: e.name,
      origin: 'employee',
      role: e.role ?? '',
      description: e.owns ?? was?.description ?? '',
      model: e.model ?? '',
      ready: was?.ready ?? true,
    })
  }
  for (const r of org) {
    if (!nodes.has(r.name)) nodes.set(r.name, { name: r.name, origin: 'chart', role: '', description: '', model: '', ready: false })
  }
  return [...nodes.values()].sort((a, b) => compare(a.name, b.name))
}

/* -- layout ---------------------------------------------------------------- */

/**
 * A top-down tree: each head over its reports, subtrees side by side. Free cards (no row in
 * the chart) sit in a row below, so a new agent is visible and ready to be dropped on a manager.
 */
export function layoutTree(nodes: readonly AgentNode[], org: readonly OrgRow[]): Map<string, Pos> {
  const known = new Set(nodes.map((n) => n.name))
  const rows = org.filter((r) => known.has(r.name))
  const inChart = new Set(rows.map((r) => r.name))
  const kids = (name: string): string[] => reportsOf(rows, name)
  const widths = new Map<string, number>()
  const widthOf = (name: string, seen: Set<string>): number => {
    if (seen.has(name)) return CARD_W
    seen.add(name)
    const below = kids(name)
    const sum = below.reduce((acc, c) => acc + widthOf(c, seen), 0) + Math.max(0, below.length - 1) * GAP_X
    const w = Math.max(CARD_W, sum)
    widths.set(name, w)
    return w
  }
  const out = new Map<string, Pos>()
  const place = (name: string, left: number, depth: number): void => {
    const w = widths.get(name) ?? CARD_W
    out.set(name, { x: left + (w - CARD_W) / 2, y: depth * (CARD_H + GAP_Y) })
    let at = left
    for (const child of kids(name)) {
      place(child, at, depth + 1)
      at += (widths.get(child) ?? CARD_W) + GAP_X
    }
  }
  const heads = rows.filter((r) => !r.reports_to || !inChart.has(r.reports_to)).map((r) => r.name).sort(compare)
  let left = 0
  const seen = new Set<string>()
  for (const head of heads) {
    widthOf(head, seen)
    place(head, left, 0)
    left += (widths.get(head) ?? CARD_W) + GAP_X * 2
  }
  const free = nodes.filter((n) => !inChart.has(n.name)).map((n) => n.name)
  const levels = rows.length ? Math.max(...rows.map((r) => levelOf(rows, r.name))) : 0
  free.forEach((name, i) => out.set(name, { x: i * (CARD_W + GAP_X), y: levels * (CARD_H + GAP_Y) + (levels ? GAP_Y / 2 : 0) }))
  return out
}

export interface View {
  x: number
  y: number
  k: number
}

export const MIN_ZOOM = 0.2
export const MAX_ZOOM = 2.5

export const clampZoom = (k: number): number => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k))

/** The view that shows every card inside a `w` x `h` box with a margin. */
export function fitView(pos: ReadonlyMap<string, Pos>, w: number, h: number, margin = 40): View {
  if (!pos.size) return { x: 0, y: 0, k: 1 }
  const xs = [...pos.values()].map((p) => p.x)
  const ys = [...pos.values()].map((p) => p.y)
  const left = Math.min(...xs)
  const top = Math.min(...ys)
  const right = Math.max(...xs) + CARD_W
  const bottom = Math.max(...ys) + CARD_H
  const k = clampZoom(Math.min((w - margin * 2) / (right - left), (h - margin * 2) / (bottom - top), 1))
  return { k, x: (w - (right - left) * k) / 2 - left * k, y: (h - (bottom - top) * k) / 2 - top * k }
}

/** Zoom about a point of the box, keeping the spot under it still. */
export function zoomAt(view: View, factor: number, cx: number, cy: number): View {
  const k = clampZoom(view.k * factor)
  const real = k / view.k
  return { k, x: cx - (cx - view.x) * real, y: cy - (cy - view.y) * real }
}

/** A card under a point given in canvas coordinates, last drawn first. */
export function cardAt(pos: ReadonlyMap<string, Pos>, x: number, y: number, skip?: string): string | null {
  let hit: string | null = null
  for (const [name, p] of pos) {
    if (name === skip) continue
    if (x >= p.x && x <= p.x + CARD_W && y >= p.y && y <= p.y + CARD_H) hit = name
  }
  return hit
}

/** Cards whose box meets a rectangle given in canvas coordinates. */
export function cardsIn(pos: ReadonlyMap<string, Pos>, x0: number, y0: number, x1: number, y1: number): string[] {
  const [l, r] = [Math.min(x0, x1), Math.max(x0, x1)]
  const [t, b] = [Math.min(y0, y1), Math.max(y0, y1)]
  return [...pos].filter(([, p]) => p.x < r && p.x + CARD_W > l && p.y < b && p.y + CARD_H > t).map(([name]) => name)
}

/* -- keyboard -------------------------------------------------------------- */

/** Every name in tree order (a head, then its reports, depth first), free cards last. */
export function treeOrder(nodes: readonly AgentNode[], org: readonly OrgRow[]): string[] {
  const known = new Set(nodes.map((n) => n.name))
  const rows = org.filter((r) => known.has(r.name))
  const inChart = new Set(rows.map((r) => r.name))
  const out: string[] = []
  const walk = (name: string): void => {
    if (out.includes(name)) return
    out.push(name)
    for (const child of reportsOf(rows, name)) walk(child)
  }
  rows.filter((r) => !r.reports_to || !inChart.has(r.reports_to)).map((r) => r.name).sort(compare).forEach(walk)
  nodes.filter((n) => !inChart.has(n.name)).forEach((n) => out.push(n.name))
  return out
}

export type Arrow = 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight'

/** Where an arrow key goes from a card: up to the manager, down to the first report, sideways along its siblings. */
export function step(org: readonly OrgRow[], at: string, key: Arrow, order: readonly string[]): string {
  const manager = byName(org).get(at)?.reports_to ?? null
  if (key === 'ArrowUp') return manager ?? at
  if (key === 'ArrowDown') return reportsOf(org, at)[0] ?? at
  const siblings = manager ? reportsOf(org, manager) : order.filter((n) => !byName(org).get(n)?.reports_to)
  const i = siblings.indexOf(at)
  if (i < 0) return at
  return siblings[Math.max(0, Math.min(siblings.length - 1, i + (key === 'ArrowRight' ? 1 : -1)))] ?? at
}

/* -- the file -------------------------------------------------------------- */

export interface GraphFile {
  version: 1
  org: OrgRow[]
  positions: Record<string, Pos>
}

export function exportGraph(org: readonly OrgRow[], positions: ReadonlyMap<string, Pos>): string {
  const file: GraphFile = { version: 1, org: org.map((r) => ({ ...r })), positions: Object.fromEntries(positions) }
  return JSON.stringify(file, null, 2)
}

/** The file's chart and positions, or null when it is not one this page wrote. */
export function importGraph(text: string): { org: OrgRow[]; positions: Map<string, Pos> } | null {
  let data: unknown
  try { data = JSON.parse(text) } catch { return null }
  const file = data as Partial<GraphFile> | null
  if (!file || file.version !== 1 || !Array.isArray(file.org)) return null
  const org: OrgRow[] = []
  for (const r of file.org) {
    if (!r || typeof r.name !== 'string' || !r.name.trim()) return null
    org.push({
      name: r.name,
      reports_to: typeof r.reports_to === 'string' && r.reports_to ? r.reports_to : null,
      department: typeof r.department === 'string' ? r.department : '',
      title: typeof r.title === 'string' ? r.title : '',
    })
  }
  const positions = new Map<string, Pos>()
  for (const [name, p] of Object.entries(file.positions ?? {})) {
    if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) positions.set(name, { x: p.x, y: p.y })
  }
  return { org, positions }
}

/** The curve a link is drawn along: from the bottom middle of the manager to the top middle of the report. */
export function wirePath(from: Pos, to: Pos): string {
  const x0 = from.x + CARD_W / 2
  const y0 = from.y + CARD_H
  const x1 = to.x + CARD_W / 2
  const y1 = to.y
  const dy = Math.max(30, Math.abs(y1 - y0) / 2)
  return `M${x0} ${y0} C${x0} ${y0 + dy} ${x1} ${y1 - dy} ${x1} ${y1}`
}
