/* The agent canvas's state and verbs.
 *
 * Page state outside React, like every domain here. What the page draws is the
 * chart (`org`), the cards (`agents`), the places the reader dragged cards to
 * (`manual`, kept in the browser) and the view. Every change to the chart goes
 * through `apply`: checked here first (graph.ts), sent whole to the engine,
 * and taken back on a refusal -- a card never sits where the engine said no.
 * The last forty chart states are kept for Ctrl+Z. */

import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import {
  MAX_DEPTH, branchOf, cardAt, cardsIn, dropFromChart, editRow, exportGraph, fitView, importGraph, layoutTree, setParent, treeOrder, zoomAt,
} from './graph'

import type { AgentNode, OrgRow, Pos, Refusal, View } from './graph'
import type { CanvasSource } from './types'

export type Tab = 'main' | 'prompt' | 'links'

export interface CanvasState {
  loaded: boolean
  loading: boolean
  /* A message the engine or the network gave, shown until the next change. */
  error: string
  /* A refusal the page made itself, as the reason; shown the same way. */
  refused: Refusal | ''
  agents: AgentNode[]
  org: OrgRow[]
  maxDepth: number
  manual: Record<string, Pos>
  selected: string[]
  /* A picked link, as "manager>report". */
  edge: string | null
  /* The card that takes the keyboard. */
  focus: string | null
  view: View
  size: { w: number; h: number }
  inspect: string | null
  tab: Tab
  undo: OrgRow[][]
  prompt: { name: string; text: string; saved: string; loading: boolean; history: string[] }
}

const UNDO_DEPTH = 40
const HISTORY_DEPTH = 20
const POS_KEY = 'rarus.canvas.positions'
const HIST_KEY = 'rarus.canvas.prompt.'

const emptyPrompt = (): CanvasState['prompt'] => ({ name: '', text: '', saved: '', loading: false, history: [] })

const initial = (): CanvasState => ({
  loaded: false,
  loading: false,
  error: '',
  refused: '',
  agents: [],
  org: [],
  maxDepth: MAX_DEPTH,
  manual: {},
  selected: [],
  edge: null,
  focus: null,
  view: { x: 40, y: 40, k: 1 },
  size: { w: 900, h: 600 },
  inspect: null,
  tab: 'main',
  undo: [],
  prompt: emptyPrompt(),
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

function savedPositions(): Record<string, Pos> {
  const raw = readJson(POS_KEY)
  const out: Record<string, Pos> = {}
  if (raw && typeof raw === 'object') {
    for (const [name, p] of Object.entries(raw as Record<string, Pos>)) {
      if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) out[name] = { x: p.x, y: p.y }
    }
  }
  return out
}

/* -- reading ---------------------------------------------------------------- */

/** Every card's place: the tree layout, with the reader's own placements over it. */
export function positions(): Map<string, Pos> {
  const s = get()
  const laid = layoutTree(s.agents, s.org)
  for (const [name, p] of Object.entries(s.manual)) if (laid.has(name)) laid.set(name, p)
  return laid
}

export const names = (): string[] => get().agents.map((a) => a.name)

export const order = (): string[] => treeOrder(get().agents, get().org)

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
    const manual = Object.fromEntries(Object.entries(savedPositions()).filter(([n]) => known.has(n)))
    patch({ loaded: true, loading: false, agents: data.agents, org: data.org, maxDepth: data.maxDepth, manual, undo: [] })
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

export function fit(): void {
  const s = get()
  patch({ view: fitView(positions(), s.size.w, s.size.h) })
}

/** Throws the reader's placements away and lays the tree out again. */
export function autoLayout(): void {
  patch({ manual: {} })
  writeJson(POS_KEY, {})
  fit()
}

/** A card dragged to a place that is not another card. */
export function placeCard(name: string, pos: Pos): void {
  const manual = { ...get().manual, [name]: pos }
  patch({ manual })
  writeJson(POS_KEY, manual)
}

/** Lets a card go back to the tree's place for it. */
export function unplace(name: string): void {
  const { [name]: _gone, ...rest } = get().manual
  patch({ manual: rest })
  writeJson(POS_KEY, rest)
}

/** The card under a canvas-coordinate point, for a drop. */
export const cardUnder = (x: number, y: number, skip?: string): string | null => cardAt(positions(), x, y, skip)

/* -- the chart -------------------------------------------------------------- */

/* The one road a chart change takes. Optimistic: the card moves at once, and the engine's
   answer -- or its refusal, which puts the old chart back -- follows. */
async function apply(next: OrgRow[]): Promise<boolean> {
  const before = get().org
  patch({ org: next, undo: [...get().undo, before].slice(-UNDO_DEPTH), error: '', refused: '' })
  try {
    patch({ org: await source().saveOrg(next) })
    return true
  } catch (e) {
    patch({ org: before, undo: get().undo.slice(0, -1), error: failure(e) })
    return false
  }
}

/** `child` now reports to `parent` (null: to nobody). Refused moves change nothing and say why. */
export async function reparent(child: string, parent: string | null): Promise<boolean> {
  const move = setParent(get().org, child, parent, names())
  if (!move.ok) {
    patch({ refused: move.reason, error: '' })
    return false
  }
  return apply(move.org)
}

/** Takes a card out of the chart: its reports move up to its manager. */
export async function removeFromChart(name: string): Promise<boolean> {
  if (!get().org.some((r) => r.name === name)) return false
  return apply(dropFromChart(get().org, name))
}

export async function editMeta(name: string, patch: Partial<Pick<OrgRow, 'department' | 'title'>>): Promise<boolean> {
  return apply(editRow(get().org, name, patch))
}

export async function undo(): Promise<boolean> {
  const stack = get().undo
  const back = stack[stack.length - 1]
  if (!back) return false
  const was = get().org
  patch({ org: back, undo: stack.slice(0, -1), error: '', refused: '' })
  try {
    patch({ org: await source().saveOrg(back) })
    return true
  } catch (e) {
    patch({ org: was, undo: stack, error: failure(e) })
    return false
  }
}

/* -- the file --------------------------------------------------------------- */

export const exportText = (): string => exportGraph(get().org, positions())

/** Replaces the chart (and the placements) with a file's. False when it is not one of ours. */
export async function importText(text: string): Promise<boolean> {
  const file = importGraph(text)
  if (!file) {
    patch({ error: 'import' })
    return false
  }
  const known = new Set(names())
  const manual = Object.fromEntries([...file.positions].filter(([n]) => known.has(n)))
  const ok = await apply(file.org)
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

