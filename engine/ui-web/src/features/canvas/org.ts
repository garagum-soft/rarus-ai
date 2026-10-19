/* Companies and departments on the canvas, as plain data (no DOM, no gateway).
 *
 * A company is a big box on the canvas; its departments are boxes inside it; employees are
 * cards inside either. The engine keeps them in `subagents.companies`
 * (raven/agent/subagent/org_store.py) and enforces who may hand work to whom
 * (raven/agent/subagent/org.py): a department talks inside itself; the company level, the
 * company head and the department heads talk to each other; nothing crosses companies; a frozen
 * company takes no work. `canTalk` mirrors that rule so the page can show it.
 *
 * Inside a company the reporting lines follow the boxes and are not drawn by hand: a department
 * member reports to the department head (or the company head), a department head and the
 * company level report to the company head. `normalize` writes those lines before every save. */

import { CARD_H, CARD_W } from './graph'

import type { OrgRow, Pos } from './graph'

export interface Department {
  name: string
  head: string
  background: string
}

export interface Company {
  id: string
  name: string
  logo: string
  head: string
  frozen: boolean
  background: string
  departments: Department[]
}

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

export const PAD = 22
export const CO_HEAD = 58
export const DEPT_HEAD = 40
export const GAP = 26
const MIN_CO_W = 420
const MIN_CO_H = 230
const MIN_DEPT_W = 286
const MIN_DEPT_BODY = 92
const COLS = 2

const compare = (a: string, b: string): number => a.localeCompare(b)

export const deptKey = (company: string, dept: string): string => `${company}/${dept}`

/** A fresh id for a new company: short, readable, not taken. */
export function newCompanyId(companies: readonly Company[], name: string): string {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24) || 'company'
  const taken = new Set(companies.map((c) => c.id))
  let id = base
  for (let i = 2; taken.has(id); i++) id = `${base}-${i}`
  return id
}

export const membersOf = (org: readonly OrgRow[], company: string, dept: string | null = null): OrgRow[] =>
  org.filter((r) => r.company === company && (dept === null || r.department === dept))

/* Heads first, then by name: the order cards take inside a box. */
function ordered(rows: readonly OrgRow[], head: string): string[] {
  return rows.map((r) => r.name).sort((a, b) => (a === head ? -1 : b === head ? 1 : compare(a, b)))
}

/** The reporting lines the boxes imply, written into every company member's row. */
export function normalize(org: readonly OrgRow[], companies: readonly Company[]): OrgRow[] {
  const byId = new Map(companies.map((c) => [c.id, c]))
  return org.map((r) => {
    const company = r.company ? byId.get(r.company) : undefined
    if (!company) return { ...r }
    const head = company.head && company.head !== r.name ? company.head : null
    if (!r.department) return { ...r, reports_to: head }
    const deptHead = company.departments.find((d) => d.name === r.department)?.head || ''
    if (deptHead === r.name || !deptHead) return { ...r, reports_to: head }
    return { ...r, reports_to: deptHead }
  })
}

/** Whether two members may hand each other work: the engine's rule, for showing it. */
export function canTalk(org: readonly OrgRow[], companies: readonly Company[], a: string, b: string): boolean {
  if (a === b) return false
  const ra = org.find((r) => r.name === a)
  const rb = org.find((r) => r.name === b)
  if (!ra || !rb || !ra.company || ra.company !== rb.company) return false
  const company = companies.find((c) => c.id === ra.company)
  if (!company) return false
  const outward = (r: OrgRow): boolean =>
    !r.department || company.head === r.name || company.departments.find((d) => d.name === r.department)?.head === r.name
  return ra.department === rb.department || (outward(ra) && outward(rb))
}

export const talksWith = (org: readonly OrgRow[], companies: readonly Company[], name: string): string[] =>
  org.filter((r) => canTalk(org, companies, name, r.name)).map((r) => r.name).sort(compare)

/** `name` moved into a company and department ('' for the company level), or out of all of them. */
export function placeMember(org: readonly OrgRow[], companies: readonly Company[], name: string, company: string, dept: string): {
  org: OrgRow[]
  companies: Company[]
} {
  const rows = org.map((r) => ({ ...r }))
  let row = rows.find((r) => r.name === name)
  if (!row) {
    row = { name, reports_to: null, department: '', title: '', company: '', avatar: '' }
    rows.push(row)
  }
  const left = row.company
  const leftDept = row.department
  row.company = company
  row.department = company ? dept : ''
  if (!company) row.reports_to = null
  const groups = companies.map((c) => ({ ...c, departments: c.departments.map((d) => ({ ...d })) }))
  if (left && (left !== company || leftDept !== row.department)) {
    const was = groups.find((c) => c.id === left)
    if (was) {
      if (was.head === name && (left !== company || row.department)) was.head = ''
      for (const d of was.departments) if (d.head === name && (left !== company || d.name !== row.department)) d.head = ''
    }
  }
  return { org: rows, companies: groups }
}

/** The company gone; its members stay on the canvas outside any company. */
export function dropCompany(org: readonly OrgRow[], companies: readonly Company[], id: string): { org: OrgRow[]; companies: Company[] } {
  return {
    org: org.map((r) => (r.company === id ? { ...r, company: '', department: '', reports_to: null } : { ...r })),
    companies: companies.filter((c) => c.id !== id),
  }
}

/** The department gone; its members move up to the company level. */
export function dropDepartment(org: readonly OrgRow[], companies: readonly Company[], id: string, dept: string): {
  org: OrgRow[]
  companies: Company[]
} {
  return {
    org: org.map((r) => (r.company === id && r.department === dept ? { ...r, department: '' } : { ...r })),
    companies: companies.map((c) => (c.id === id ? { ...c, departments: c.departments.filter((d) => d.name !== dept) } : c)),
  }
}

export function renameDepartment(org: readonly OrgRow[], companies: readonly Company[], id: string, from: string, to: string): {
  org: OrgRow[]
  companies: Company[]
} {
  return {
    org: org.map((r) => (r.company === id && r.department === from ? { ...r, department: to } : { ...r })),
    companies: companies.map((c) => (c.id === id
      ? { ...c, departments: c.departments.map((d) => (d.name === from ? { ...d, name: to } : d)) }
      : c)),
  }
}

/* -- layout ------------------------------------------------------------------------- */

export interface Boxes {
  /** Every company member's card, by name. */
  cards: Map<string, Pos>
  companies: Map<string, Rect>
  /** By `deptKey(company, name)`. */
  depts: Map<string, Rect>
}

const union = (a: Rect, b: Rect): Rect => {
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  return { x, y, w: Math.max(a.x + a.w, b.x + b.w) - x, h: Math.max(a.y + a.h, b.y + b.h) - y }
}

const cardRect = (p: Pos): Rect => ({ x: p.x, y: p.y, w: CARD_W, h: CARD_H })
const grow = (r: Rect, left: number, top: number, right: number, bottom: number): Rect =>
  ({ x: r.x - left, y: r.y - top, w: r.w + left + right, h: r.h + top + bottom })

/**
 * Where every company, department and member card sits. A box starts at its anchor (stored after a
 * drag, or the default one) with a minimum size, and grows to hold every card dragged inside it.
 * Default anchors: companies side by side to the right of `startX`; departments in a row under the
 * company's own members. Default card places: a row at the company level, two columns in a department.
 */
export function layoutCompanies(
  org: readonly OrgRow[],
  companies: readonly Company[],
  manual: Readonly<Record<string, Pos>>,
  anchors: Readonly<Record<string, Pos>>,
  startX: number,
): Boxes {
  const cards = new Map<string, Pos>()
  const coRects = new Map<string, Rect>()
  const deptRects = new Map<string, Rect>()
  let nextX = startX
  for (const company of companies) {
    const at = anchors[`c:${company.id}`] ?? { x: nextX, y: 0 }
    const top = membersOf(org, company.id, '')
    const topNames = ordered(top, company.head)
    let box: Rect = { x: at.x, y: at.y, w: MIN_CO_W, h: MIN_CO_H }
    topNames.forEach((name, i) => {
      const p = manual[name] ?? { x: at.x + PAD + i * (CARD_W + GAP), y: at.y + CO_HEAD + PAD }
      cards.set(name, p)
      box = union(box, grow(cardRect(p), PAD, CO_HEAD, PAD, PAD))
    })
    let deptX = at.x + PAD
    const deptY = at.y + CO_HEAD + PAD + (topNames.length ? CARD_H + GAP + 8 : 0)
    for (const dept of company.departments) {
      const key = deptKey(company.id, dept.name)
      const dAt = anchors[`d:${key}`] ?? { x: deptX, y: deptY }
      const names = ordered(membersOf(org, company.id, dept.name), dept.head)
      const cols = Math.max(1, Math.min(COLS, names.length))
      const rows = Math.max(1, Math.ceil(names.length / COLS))
      let dRect: Rect = {
        x: dAt.x,
        y: dAt.y,
        w: Math.max(MIN_DEPT_W, cols * CARD_W + (cols - 1) * GAP + PAD * 2),
        h: DEPT_HEAD + Math.max(MIN_DEPT_BODY, rows * CARD_H + (rows - 1) * GAP) + PAD,
      }
      names.forEach((name, i) => {
        const p = manual[name] ?? { x: dAt.x + PAD + (i % COLS) * (CARD_W + GAP), y: dAt.y + DEPT_HEAD + Math.floor(i / COLS) * (CARD_H + GAP) }
        cards.set(name, p)
        dRect = union(dRect, grow(cardRect(p), PAD, DEPT_HEAD, PAD, PAD))
      })
      deptRects.set(key, dRect)
      box = union(box, grow(dRect, PAD, CO_HEAD, PAD, PAD))
      deptX = Math.max(deptX, dRect.x + dRect.w) + GAP
    }
    coRects.set(company.id, box)
    nextX = Math.max(nextX, box.x + box.w) + GAP * 3
  }
  return { cards, companies: coRects, depts: deptRects }
}

const inside = (r: Rect, x: number, y: number): boolean => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h

/** The innermost box under a canvas point: a department, else a company, else nothing. */
export function boxAt(boxes: Boxes, x: number, y: number): { company: string; dept: string } | null {
  for (const [key, r] of boxes.depts) {
    if (inside(r, x, y)) {
      const cut = key.indexOf('/')
      return { company: key.slice(0, cut), dept: key.slice(cut + 1) }
    }
  }
  for (const [id, r] of boxes.companies) if (inside(r, x, y)) return { company: id, dept: '' }
  return null
}

/* -- export ------------------------------------------------------------------------- */

export interface CompanyFile {
  version: 1
  kind: 'company'
  company: Company
  members: OrgRow[]
}

export function exportCompany(org: readonly OrgRow[], companies: readonly Company[], id: string): string {
  const company = companies.find((c) => c.id === id)
  if (!company) return ''
  const file: CompanyFile = { version: 1, kind: 'company', company, members: membersOf(org, id).map((r) => ({ ...r })) }
  return JSON.stringify(file, null, 2)
}
