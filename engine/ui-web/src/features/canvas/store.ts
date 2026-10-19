/* The agent canvas's state and verbs.
 *
 * Page state outside React, like every domain here. What the page draws is the
 * chart (`org`), the companies and departments it is grouped into (`companies`),
 * the cards (`agents`), the places the reader dragged cards and boxes to
 * (`manual`, `anchors`, kept in the browser) and the view. Every change to the
 * chart goes through `applyAll`: checked here first (graph.ts, org.ts), sent whole
 * to the engine, and taken back on a refusal -- a card never sits where the engine
 * said no. The last forty states are kept for Ctrl+Z. */

import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { DEFAULT_COMPANY_BG, DEFAULT_DEPT_BG } from './art'
import {
  CARD_H, CARD_W, MAX_DEPTH, branchOf, cardAt, cardsIn, dropFromChart, editRow, exportGraph, fitView, importGraph, layoutTree, setParent,
  treeOrder, zoomAt,
} from './graph'
import {
  deptKey, dropCompany, dropDepartment, exportCompany, layoutCompanies, membersOf, newCompanyId, normalize, placeMember, renameDepartment,
} from './org'

import type { AgentNode, OrgRow, Pos, Refusal, View } from './graph'
import type { Boxes, Company, Department } from './org'
import type { CanvasSource, HireSpec } from './types'

export type Tab = 'main' | 'prompt' | 'links'

interface Snapshot {
  org: OrgRow[]
  companies: Company[]
}

export interface CanvasState {
  loaded: boolean
  loading: boolean
  /* A message the engine or the network gave, shown until the next change. */
  error: string
  /* A refusal the page made itself, as the reason; shown the same way. */
  refused: Refusal | ''
  agents: AgentNode[]
  org: OrgRow[]
  companies: Company[]
  maxDepth: number
  manual: Record<string, Pos>
  /* Where the reader dragged a company (`c:<id>`) or a department (`d:<id>/<name>`) to. */
  anchors: Record<string, Pos>
  selected: string[]
  /* A picked link, as "manager>report". */
  edge: string | null
  /* The card that takes the keyboard. */
  focus: string | null
  view: View
  size: { w: number; h: number }
  inspect: string | null
  tab: Tab
  undo: Snapshot[]
  prompt: { name: string; text: string; saved: string; loading: boolean; history: string[] }
  /* A hire on its way to the engine: the dialog waits on it. */
  busy: boolean
}

const UNDO_DEPTH = 40
const HISTORY_DEPTH = 20
const POS_KEY = 'rarus.canvas.positions'
const BOX_KEY = 'rarus.canvas.boxes'
const HIST_KEY = 'rarus.canvas.prompt.'
/* The room between the free cards' tree and the first company. */
const COMPANY_GAP = 140

const emptyPrompt = (): CanvasState['prompt'] => ({ name: '', text: '', saved: '', loading: false, history: [] })

const initial = (): CanvasState => ({
  loaded: false,
  loading: false,
  error: '',
  refused: '',
  agents: [],
  org: [],
  companies: [],
  maxDepth: MAX_DEPTH,
  manual: {},
  anchors: {},
  selected: [],
  edge: null,
  focus: null,
  view: { x: 40, y: 40, k: 1 },
  size: { w: 900, h: 600 },
  inspect: null,
  tab: 'main',
  undo: [],
  prompt: emptyPrompt(),
  busy: false,
})

const store = makeStore<CanvasState>(initial())

export const { get, set, subscribe, _resetForTests } = store

const patch = (change: Partial<CanvasState>): void => store.set((prev) => ({ ...prev, ...change }))

const source = (): CanvasSource => ds('canvas')

const failure = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string } | null
  return (err && ((err.data && err.data.detail) || err.message)) || String(e)
}

/* -- browser storage: only ever a convenience ------------------------------ */

const readJson = (key: string): unknown => {
  try { return JSON.parse(localStorage.getItem(key) || 'null') } catch { return null }
}
const writeJson = (key: string, value: unknown): void => {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode or over quota */ }
}

function savedPlaces(key: string): Record<string, Pos> {
  const raw = readJson(key)
  const out: Record<string, Pos> = {}
  if (raw && typeof raw === 'object') {
    for (const [name, p] of Object.entries(raw as Record<string, Pos>)) {
      if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) out[name] = { x: p.x, y: p.y }
    }
  }
  return out
}

/* -- reading ---------------------------------------------------------------- */

export interface Layout {
  cards: Map<string, Pos>
  boxes: Boxes
}

/**
 * Every card's and box's place: the free cards (no company) as the tree with the reader's own
 * placements over it, the companies to its right with their departments and members (org.ts).
 */
export function layout(): Layout {
  const s = get()
  const inCompany = new Set(s.org.filter((r) => r.company).map((r) => r.name))
  const cards = layoutTree(s.agents.filter((a) => !inCompany.has(a.name)), s.org.filter((r) => !r.company))
  for (const [name, p] of Object.entries(s.manual)) if (cards.has(name)) cards.set(name, p)
  const right = cards.size ? Math.max(...[...cards.values()].map((p) => p.x)) + CARD_W + COMPANY_GAP : 0
  const known = new Set(s.agents.map((a) => a.name))
  const boxes = layoutCompanies(s.org.filter((r) => known.has(r.name)), s.companies, s.manual, s.anchors, right)
  for (const [name, p] of boxes.cards) cards.set(name, p)
  return { cards, boxes }
}

export function positions(): Map<string, Pos> {
  return layout().cards
}

export const names = (): string[] => get().agents.map((a) => a.name)

export const order = (): string[] => treeOrder(get().agents, get().org)

export const rowOf = (name: string): OrgRow | undefined => get().org.find((r) => r.name === name)

export const companyOf = (id: string): Company | undefined => get().companies.find((c) => c.id === id)

/** The cards that stay lit: the picked card's line, or everyone when nothing is picked. */
export function lit(): Set<string> | null {
  const s = get()
  if (!s.selected.length) return null
  const out = new Set<string>()
  for (const name of s.selected) for (const n of branchOf(s.org, name)) out.add(n)
  return out
}

export async function load(): Promise<void> {
  if (get().loading) return
  patch({ loading: true, error: '' })
  try {
    const data = await source().load()
    const known = new Set(data.agents.map((a) => a.name))
    const manual = Object.fromEntries(Object.entries(savedPlaces(POS_KEY)).filter(([n]) => known.has(n)))
    patch({
      loaded: true, loading: false, agents: data.agents, org: data.org, companies: data.companies, maxDepth: data.maxDepth, manual,
      anchors: savedPlaces(BOX_KEY), undo: [],
    })
  } catch (e) {
    patch({ loaded: true, loading: false, error: failure(e) })
  }
}

export function open(): void {
  page.show('canvasPage')
  void load()
}

export function close(): void {
  page.show(null)
}

/* -- selection and focus --------------------------------------------------- */

export function select(name: string, additive = false): void {
  const s = get()
  const next = additive
    ? (s.selected.includes(name) ? s.selected.filter((n) => n !== name) : [...s.selected, name])
    : [name]
  patch({ selected: next, edge: null, focus: name, refused: '', error: '' })
}

export function clearSelection(): void {
  patch({ selected: [], edge: null, inspect: null })
}

/** Picks a link ("manager>report"), lighting its two cards and nothing else; null lets go. */
export function selectEdge(key: string | null): void {
  const ends = key ? key.split('>') : []
  patch({ edge: key, selected: ends, refused: '', error: '' })
}

/** Removes the picked link: the report now reports to nobody. */
export async function unlinkSelected(): Promise<boolean> {
  const key = get().edge
  if (!key) return false
  const report = key.slice(key.indexOf('>') + 1)
  patch({ edge: null })
  return reparent(report, null)
}

export function selectMany(list: string[], additive = false): void {
  const s = get()
  patch({ selected: additive ? [...new Set([...s.selected, ...list])] : list })
}

export function selectInBox(x0: number, y0: number, x1: number, y1: number, additive = false): void {
  selectMany(cardsIn(positions(), x0, y0, x1, y1), additive)
}

export function focusOn(name: string): void {
  patch({ focus: name })
}

export function clearError(): void {
  patch({ error: '', refused: '' })
}

/* -- the view -------------------------------------------------------------- */

export function setSize(w: number, h: number): void {
  const s = get().size
  if (s.w !== w || s.h !== h) patch({ size: { w, h } })
}

export function setView(view: View): void {
  patch({ view })
}

export function panBy(dx: number, dy: number): void {
  const v = get().view
  patch({ view: { ...v, x: v.x + dx, y: v.y + dy } })
}

export function zoomBy(factor: number, cx?: number, cy?: number): void {
  const s = get()
  patch({ view: zoomAt(s.view, factor, cx ?? s.size.w / 2, cy ?? s.size.h / 2) })
}

/** The view that shows every card and every box. */
export function fit(): void {
  const s = get()
  const { cards, boxes } = layout()
  const corners = new Map(cards)
  for (const [id, r] of [...boxes.companies, ...boxes.depts]) {
    corners.set(`<${id}`, { x: r.x, y: r.y })
    corners.set(`>${id}`, { x: r.x + r.w - CARD_W, y: r.y + r.h - CARD_H })
  }
  patch({ view: fitView(corners, s.size.w, s.size.h) })
}

/** Throws the reader's placements away and lays everything out again. */
export function autoLayout(): void {
  patch({ manual: {}, anchors: {} })
  writeJson(POS_KEY, {})
  writeJson(BOX_KEY, {})
  fit()
}

/** A card dragged to a place that is not another card. */
export function placeCard(name: string, pos: Pos): void {
  const manual = { ...get().manual, [name]: pos }
  patch({ manual })
  writeJson(POS_KEY, manual)
}

/** Lets a card go back to the layout's place for it. */
export function unplace(name: string): void {
  const { [name]: _gone, ...rest } = get().manual
  patch({ manual: rest })
  writeJson(POS_KEY, rest)
}

/** The card under a canvas-coordinate point, for a drop. */
export const cardUnder = (x: number, y: number, skip?: string): string | null => cardAt(positions(), x, y, skip)

/* -- moving whole boxes ------------------------------------------------------ */

export type BoxRef = { kind: 'company'; id: string } | { kind: 'dept'; id: string; dept: string }

/** Where a box and everything in it stand now: what a drag moves from. */
export interface BoxGrip {
  anchors: Record<string, Pos>
  cards: Record<string, Pos>
}

export function gripBox(ref: BoxRef): BoxGrip {
  const { cards, boxes } = layout()
  const s = get()
  const anchors: Record<string, Pos> = {}
  const members: Record<string, Pos> = {}
  const take = (key: string, rect: { x: number; y: number } | undefined): void => { if (rect) anchors[key] = { x: rect.x, y: rect.y } }
  if (ref.kind === 'company') {
    const company = s.companies.find((c) => c.id === ref.id)
    take(`c:${ref.id}`, s.anchors[`c:${ref.id}`] ?? boxes.companies.get(ref.id))
    for (const d of company?.departments ?? []) take(`d:${deptKey(ref.id, d.name)}`, boxes.depts.get(deptKey(ref.id, d.name)))
    for (const r of membersOf(s.org, ref.id)) { const p = cards.get(r.name); if (p) members[r.name] = p }
  } else {
    take(`d:${deptKey(ref.id, ref.dept)}`, boxes.depts.get(deptKey(ref.id, ref.dept)))
    for (const r of membersOf(s.org, ref.id, ref.dept)) { const p = cards.get(r.name); if (p) members[r.name] = p }
  }
  return { anchors, cards: members }
}

/** Moves a gripped box by (dx, dy) from where it was gripped; `save` keeps it in the browser. */
export function moveBox(grip: BoxGrip, dx: number, dy: number, save = false): void {
  const s = get()
  const anchors = { ...s.anchors }
  const manual = { ...s.manual }
  for (const [key, p] of Object.entries(grip.anchors)) anchors[key] = { x: p.x + dx, y: p.y + dy }
  for (const [name, p] of Object.entries(grip.cards)) manual[name] = { x: p.x + dx, y: p.y + dy }
  patch({ anchors, manual })
  if (save) {
    writeJson(BOX_KEY, anchors)
    writeJson(POS_KEY, manual)
  }
}

/* -- the chart -------------------------------------------------------------- */

/* The one road a change takes. Optimistic: the card moves at once, and the engine's answer -- or
   its refusal, which puts the old state back -- follows. Reporting lines inside companies are
   written from the boxes first (org.ts `normalize`). */
async function applyAll(org: OrgRow[], companies: Company[]): Promise<boolean> {
  const next = normalize(org, companies)
  const before: Snapshot = { org: get().org, companies: get().companies }
  patch({ org: next, companies, undo: [...get().undo, before].slice(-UNDO_DEPTH), error: '', refused: '' })
  try {
    const saved = await source().saveOrg(next, companies)
    patch({ org: saved.org, companies: saved.companies })
    return true
  } catch (e) {
    patch({ org: before.org, companies: before.companies, undo: get().undo.slice(0, -1), error: failure(e) })
    return false
  }
}

const apply = (next: OrgRow[]): Promise<boolean> => applyAll(next, get().companies)

/** `child` now reports to `parent` (null: to nobody). Refused moves change nothing and say why. */
export async function reparent(child: string, parent: string | null): Promise<boolean> {
  const move = setParent(get().org, child, parent, names())
  if (!move.ok) {
    patch({ refused: move.reason, error: '' })
    return false
  }
  return apply(move.org)
}

/** Takes a card out of the chart: its reports move up to its manager, a head's post is left empty. */
export async function removeFromChart(name: string): Promise<boolean> {
  if (!get().org.some((r) => r.name === name)) return false
  const companies = get().companies.map((c) => ({
    ...c,
    head: c.head === name ? '' : c.head,
    departments: c.departments.map((d) => (d.head === name ? { ...d, head: '' } : d)),
  }))
  return applyAll(dropFromChart(get().org, name), companies)
}

export async function editMeta(name: string, change: Partial<Pick<OrgRow, 'department' | 'title' | 'avatar'>>): Promise<boolean> {
  return apply(editRow(get().org, name, change))
}

export async function undo(): Promise<boolean> {
  const stack = get().undo
  const back = stack[stack.length - 1]
  if (!back) return false
  const was: Snapshot = { org: get().org, companies: get().companies }
  patch({ org: back.org, companies: back.companies, undo: stack.slice(0, -1), error: '', refused: '' })
  try {
    const saved = await source().saveOrg(back.org, back.companies)
    patch({ org: saved.org, companies: saved.companies })
    return true
  } catch (e) {
    patch({ org: was.org, companies: was.companies, undo: stack, error: failure(e) })
    return false
  }
}

/* -- companies and departments --------------------------------------------------- */

const copyCompanies = (): Company[] => get().companies.map((c) => ({ ...c, departments: c.departments.map((d) => ({ ...d })) }))

function setAnchor(key: string, at: Pos | undefined): void {
  if (!at) return
  const anchors = { ...get().anchors, [key]: at }
  patch({ anchors })
  writeJson(BOX_KEY, anchors)
}

/** A new company box at `at` (canvas coordinates); returns its id, or '' when the engine said no. */
export async function addCompany(name: string, logo: string, at?: Pos): Promise<string> {
  const clean = name.trim()
  if (!clean) return ''
  const id = newCompanyId(get().companies, clean)
  const company: Company = { id, name: clean, logo, head: '', frozen: false, background: DEFAULT_COMPANY_BG, departments: [] }
  setAnchor(`c:${id}`, at)
  return (await applyAll(get().org, [...copyCompanies(), company])) ? id : ''
}

export async function updateCompany(id: string, change: Partial<Pick<Company, 'name' | 'logo' | 'frozen' | 'background'>>): Promise<boolean> {
  return applyAll(get().org, copyCompanies().map((c) => (c.id === id ? { ...c, ...change } : c)))
}

export async function deleteCompany(id: string): Promise<boolean> {
  const company = companyOf(id)
  const out = dropCompany(get().org, get().companies, id)
  const ok = await applyAll(out.org, out.companies)
  if (ok) {
    const anchors = { ...get().anchors }
    delete anchors[`c:${id}`]
    for (const d of company?.departments ?? []) delete anchors[`d:${deptKey(id, d.name)}`]
    patch({ anchors })
    writeJson(BOX_KEY, anchors)
  }
  return ok
}

/** A new department in a company; false (and a notice) when the company already has one by that name. */
export async function addDepartment(id: string, name: string, at?: Pos): Promise<boolean> {
  const clean = name.trim()
  const company = companyOf(id)
  if (!clean || !company) return false
  if (company.departments.some((d) => d.name === clean)) {
    patch({ error: 'dept_taken' })
    return false
  }
  const dept: Department = { name: clean, head: '', background: DEFAULT_DEPT_BG }
  setAnchor(`d:${deptKey(id, clean)}`, at)
  return applyAll(get().org, copyCompanies().map((c) => (c.id === id ? { ...c, departments: [...c.departments, dept] } : c)))
}

export async function updateDepartment(id: string, dept: string, change: Partial<Pick<Department, 'name' | 'background'>>): Promise<boolean> {
  let org = get().org
  let companies = copyCompanies()
  if (change.name !== undefined && change.name.trim() && change.name.trim() !== dept) {
    const to = change.name.trim()
    if (companyOf(id)?.departments.some((d) => d.name === to)) {
      patch({ error: 'dept_taken' })
      return false
    }
    const moved = renameDepartment(org, companies, id, dept, to)
    org = moved.org
    companies = moved.companies
    const anchors = { ...get().anchors }
    const was = anchors[`d:${deptKey(id, dept)}`]
    if (was) {
      delete anchors[`d:${deptKey(id, dept)}`]
      anchors[`d:${deptKey(id, to)}`] = was
      patch({ anchors })
      writeJson(BOX_KEY, anchors)
    }
    dept = to
  }
  if (change.background !== undefined) {
    const bg = change.background
    companies = companies.map((c) => (c.id === id
      ? { ...c, departments: c.departments.map((d) => (d.name === dept ? { ...d, background: bg } : d)) }
      : c))
  }
  return applyAll(org, companies)
}

export async function deleteDepartment(id: string, dept: string): Promise<boolean> {
  const out = dropDepartment(get().org, get().companies, id, dept)
  return applyAll(out.org, out.companies)
}

/** Makes `name` the head of where it works: its department, or the company at the company level. */
export async function makeHead(name: string): Promise<boolean> {
  const row = rowOf(name)
  if (!row?.company) return false
  const companies = copyCompanies().map((c) => {
    if (c.id !== row.company) return c
    if (!row.department) return { ...c, head: name }
    return { ...c, departments: c.departments.map((d) => (d.name === row.department ? { ...d, head: name } : d)) }
  })
  return applyAll(get().org, companies)
}

/** Moves a card into a company and department ('' for the company level), or out of every company. */
export async function moveMember(name: string, company: string, dept: string): Promise<boolean> {
  const row = rowOf(name)
  if (row && row.company === company && row.department === (company ? dept : '')) return true
  const out = placeMember(get().org, get().companies, name, company, dept)
  if (!company) unplace(name)
  return applyAll(out.org, out.companies)
}

export async function setAvatar(name: string, avatar: string): Promise<boolean> {
  return editMeta(name, { avatar })
}

/** Hires a new employee straight into a company or department ('' and '' for none). */
export async function hire(spec: HireSpec, company: string, dept: string, avatar: string): Promise<boolean> {
  patch({ busy: true, error: '' })
  let agent: AgentNode
  try {
    agent = await source().hire(spec)
  } catch (e) {
    patch({ busy: false, error: failure(e) })
    return false
  } finally {
    patch({ busy: false })
  }
  patch({ agents: [...get().agents.filter((a) => a.name !== agent.name), agent].sort((a, b) => a.name.localeCompare(b.name)) })
  const out = placeMember(get().org, get().companies, agent.name, company, dept)
  const org = out.org.map((r) => (r.name === agent.name ? { ...r, avatar } : r))
  return applyAll(org, out.companies)
}

/** Lets an employee go: the engine keeps its folder aside, the card and its row leave the canvas. */
export async function retire(name: string): Promise<boolean> {
  try {
    await source().retire(name)
  } catch (e) {
    patch({ error: failure(e) })
    return false
  }
  patch({ agents: get().agents.filter((a) => a.name !== name), inspect: get().inspect === name ? null : get().inspect })
  unplace(name)
  return removeFromChart(name)
}

/* -- the file --------------------------------------------------------------- */

export const exportText = (): string => exportGraph(get().org, positions(), get().companies)

export const exportCompanyText = (id: string): string => exportCompany(get().org, get().companies, id)

const asCompany = (raw: unknown): Company | null => {
  const c = raw as Partial<Company> | null
  if (!c || typeof c.id !== 'string' || typeof c.name !== 'string' || !c.id || !c.name.trim()) return null
  const text = (v: unknown): string => (typeof v === 'string' ? v : '')
  return {
    id: c.id,
    name: c.name,
    logo: text(c.logo),
    head: text(c.head),
    frozen: !!c.frozen,
    background: text(c.background),
    departments: Array.isArray(c.departments)
      ? c.departments.filter((d) => d && typeof d.name === 'string' && d.name).map((d) => ({ name: d.name, head: text(d.head), background: text(d.background) }))
      : [],
  }
}

/* A company file (org.ts `exportCompany`) is added as a new company, its members moved into it. */
async function importCompany(data: { company?: unknown; members?: unknown }): Promise<boolean> {
  const company = asCompany(data.company)
  if (!company || !Array.isArray(data.members)) {
    patch({ error: 'import' })
    return false
  }
  const id = newCompanyId(get().companies, company.name)
  let org = get().org.map((r) => ({ ...r }))
  const incoming = (data.members as Array<Partial<OrgRow>>).filter((m) => m && typeof m.name === 'string' && m.name)
  for (const m of incoming) {
    const row: OrgRow = {
      name: m.name as string, reports_to: null, department: typeof m.department === 'string' ? m.department : '',
      title: typeof m.title === 'string' ? m.title : '', company: id, avatar: typeof m.avatar === 'string' ? m.avatar : '',
    }
    org = [...org.filter((r) => r.name !== row.name), row]
  }
  const known = new Set(incoming.map((m) => m.name))
  const fixed: Company = {
    ...company,
    id,
    head: known.has(company.head) ? company.head : '',
    departments: company.departments.map((d) => ({ ...d, head: known.has(d.head) ? d.head : '' })),
  }
  return applyAll(org, [...copyCompanies(), fixed])
}

/** Replaces the chart (and the placements) with a file's, or adds a company file's company. False when it is neither. */
export async function importText(text: string): Promise<boolean> {
  let raw: unknown
  try { raw = JSON.parse(text) } catch { raw = null }
  if (raw && typeof raw === 'object' && (raw as { kind?: unknown }).kind === 'company') return importCompany(raw as { company?: unknown; members?: unknown })
  const file = importGraph(text)
  if (!file) {
    patch({ error: 'import' })
    return false
  }
  const known = new Set(names())
  const manual = Object.fromEntries([...file.positions].filter(([n]) => known.has(n)))
  const companies = file.companies.map(asCompany).filter((c): c is Company => !!c)
  const ok = await applyAll(file.org, companies)
  if (ok) {
    patch({ manual })
    writeJson(POS_KEY, manual)
  }
  return ok
}

/* -- the inspector ----------------------------------------------------------- */

export function openInspector(name: string, tab: Tab = 'main'): void {
  patch({ inspect: name, tab, selected: [name], focus: name })
  if (tab === 'prompt') void loadPrompt(name)
}

export function closeInspector(): void {
  patch({ inspect: null })
}

export function setTab(tab: Tab): void {
  patch({ tab })
  const name = get().inspect
  if (tab === 'prompt' && name) void loadPrompt(name)
}

export const isEmployee = (name: string): boolean => get().agents.find((a) => a.name === name)?.origin === 'employee'

export const historyOf = (name: string): string[] => {
  const raw = readJson(HIST_KEY + name)
  return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : []
}

export async function loadPrompt(name: string): Promise<void> {
  if (!isEmployee(name)) {
    patch({ prompt: emptyPrompt() })
    return
  }
  patch({ prompt: { name, text: '', saved: '', loading: true, history: historyOf(name) } })
  try {
    const text = await source().promptGet(name)
    if (get().prompt.name === name) patch({ prompt: { name, text, saved: text, loading: false, history: historyOf(name) } })
  } catch (e) {
    if (get().prompt.name === name) patch({ prompt: { ...get().prompt, loading: false }, error: failure(e) })
  }
}

export function editPrompt(text: string): void {
  patch({ prompt: { ...get().prompt, text } })
}

/** Saves the draft; the text it replaces goes into the agent's history first. */
export async function savePrompt(): Promise<boolean> {
  const p = get().prompt
  if (!p.name || p.text === p.saved) return false
  try {
    if (!(await source().promptSet(p.name, p.text))) throw new Error('not saved')
  } catch (e) {
    patch({ error: failure(e) })
    return false
  }
  const history = [p.saved, ...historyOf(p.name).filter((x) => x !== p.saved)].filter(Boolean).slice(0, HISTORY_DEPTH)
  writeJson(HIST_KEY + p.name, history)
  patch({ prompt: { ...p, saved: p.text, history }, error: '' })
  return true
}

/** Puts a version from the history back into the draft; saving it is the reader's next step. */
export function rollbackTo(index: number): void {
  const p = get().prompt
  const text = p.history[index]
  if (text !== undefined) patch({ prompt: { ...p, text } })
}
