import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import * as lang from '../../state/lang'
import * as page from '../../state/page'
import { DEFAULT_COMPANY_BG, DEFAULT_DEPT_BG, avatarSrc, backdropCss, letterLogo } from './art'
import { CARD_H, CARD_W, byName, chain, edgesOf, levelOf, managersFor, reportsOf, step, wirePath } from './graph'
import { AvatarDialog, BackdropDialog, CompanyDialog, ContextMenu, HeadDialog, HireDialog, NameDialog } from './CanvasMenus'
import { boxAt, deptKey, membersOf, talksWith } from './org'
import * as store from './store'

import type { Arrow, Pos } from './graph'
import type { HireForm, MenuItem } from './CanvasMenus'
import type { Company } from './org'
import type { BoxGrip, BoxRef, Tab } from './store'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from 'react'
import './styles.css'

/* The agent hierarchy as a graph of companies, departments and cards. Right-click empty space to
   add a company, a company to add a department or an employee, a department to add an employee
   there, a card for what can be done with it; double-click a card for its panel. Drag a card into
   a department or a company to move it there, out of every box to take it out of the company;
   drag a box by its header to move it with everything in it. Pan by dragging empty space, zoom with
   the wheel, Shift-drag to pick several. Keys: Tab into the tree, arrows along it, Enter for the
   panel, Delete to take a card out, Esc to let go, Ctrl+Z to undo. */

const DRAG_SLOP = 4
const arrows: readonly string[] = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']

const REFUSALS = {
  self: 'gui.canvas.refuse_self',
  cycle: 'gui.canvas.refuse_cycle',
  depth: 'gui.canvas.refuse_depth',
  unknown: 'gui.canvas.refuse_unknown',
} as const

/* Taking a card out of the chart moves its reports up, so the reader is asked first, in the app's
   own dialog. */
const askRemove = (name: string): void =>
  confirm.ask(t('gui.canvas.remove'), t('gui.canvas.confirm_remove', { name }), t('gui.canvas.remove'), () => { void store.removeFromChart(name) })

const askRetire = (name: string): void =>
  confirm.ask(t('gui.canvas.m_retire'), t('gui.canvas.confirm_retire', { name }), t('gui.canvas.m_retire'), () => { void store.retire(name) })

interface Drag {
  mode: 'pan' | 'card' | 'box' | 'move'
  /* Pointer at the start, in screen pixels; the card or view it moved from. */
  sx: number
  sy: number
  from: Pos
  name: string
  moved: boolean
  additive: boolean
  grip?: BoxGrip
}

type Target =
  | { kind: 'empty' }
  | { kind: 'company'; id: string }
  | { kind: 'dept'; id: string; dept: string }
  | { kind: 'card'; name: string }

interface MenuAt {
  x: number
  y: number
  at: Pos
  target: Target
}

type DialogState =
  | { kind: 'company-new'; at: Pos }
  | { kind: 'company-edit'; id: string }
  | { kind: 'company-rename'; id: string }
  | { kind: 'dept-new'; id: string; at?: Pos }
  | { kind: 'dept-rename'; id: string; dept: string }
  | { kind: 'hire'; id: string; dept: string }
  | { kind: 'avatar'; name: string }
  | { kind: 'bg-company'; id: string }
  | { kind: 'bg-dept'; id: string; dept: string }
  | { kind: 'head'; id: string; dept: string }

/* Nothing is drawn while the page is shut: the island is mounted at boot with the rest, and a
   canvas painted into a closed page would cost every first paint a tree nobody can see. */
export function CanvasApp(): JSX.Element | null {
  useSyncExternalStore(lang.subscribe, lang.get)
  const shown = useSyncExternalStore(page.subscribe, page.get)
  return shown === 'canvasPage' ? <CanvasBody /> : null
}

function targetOf(el: HTMLElement): Target {
  const card = el.closest<HTMLElement>('[data-card]')
  if (card) return { kind: 'card', name: card.dataset.card as string }
  const dept = el.closest<HTMLElement>('[data-dept]')
  if (dept) return { kind: 'dept', id: dept.dataset.co as string, dept: dept.dataset.dept as string }
  const co = el.closest<HTMLElement>('[data-company]')
  if (co) return { kind: 'company', id: co.dataset.company as string }
  return { kind: 'empty' }
}

function CanvasBody(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const [panel, setPanel] = useState<string | null>(null)
  const stage = useRef<HTMLDivElement>(null)
  const drag = useRef<Drag | null>(null)
  const [box, setBox] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [dropAt, setDropAt] = useState<string | null>(null)
  const [menu, setMenu] = useState<MenuAt | null>(null)
  const [dialog, setDialog] = useState<DialogState | null>(null)
  const closeMenu = useCallback(() => setMenu(null), [])

  /* The box the view is fitted to follows the stage's real size. */
  useEffect(() => {
    const el = stage.current
    if (!el) return
    const measure = (): void => store.setSize(el.clientWidth || 900, el.clientHeight || 600)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(measure)
    watch.observe(el)
    return () => watch.disconnect()
  }, [])

  /* A wheel listener of our own: React's is passive, and the page behind must not scroll. */
  useEffect(() => {
    const el = stage.current
    if (!el) return
    const wheel = (e: WheelEvent): void => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      store.zoomBy(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - rect.left, e.clientY - rect.top)
    }
    el.addEventListener('wheel', wheel, { passive: false })
    return () => el.removeEventListener('wheel', wheel)
  }, [])

  const { cards: pos, boxes } = store.layout()
  const lit = store.lit()
  const order = store.order()
  const rows = byName(s.org)
  /* Inside a company the boxes say who reports to whom, so only the free cards' links are drawn. */
  const edges = edgesOf(s.org).filter((e) => pos.has(e.from) && pos.has(e.to) && !rows.get(e.to)?.company)
  const known = s.agents.map((a) => a.name)

  const toCanvas = (clientX: number, clientY: number): Pos => {
    const rect = stage.current?.getBoundingClientRect()
    return {
      x: (clientX - (rect?.left ?? 0) - s.view.x) / s.view.k,
      y: (clientY - (rect?.top ?? 0) - s.view.y) / s.view.k,
    }
  }

  const onDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return
    const el = e.target as HTMLElement
    const target = el.closest<HTMLElement>('[data-card]')
    const wire = el.closest<HTMLElement>('[data-wire]')
    const grip = el.closest<HTMLElement>('[data-grip]')
    if (wire) return
    if (target) {
      const name = target.dataset.card as string
      const at = pos.get(name) as Pos
      drag.current = { mode: 'card', sx: e.clientX, sy: e.clientY, from: at, name, moved: false, additive: e.shiftKey }
    } else if (grip) {
      const ref: BoxRef = grip.dataset.dept
        ? { kind: 'dept', id: grip.dataset.co as string, dept: grip.dataset.dept }
        : { kind: 'company', id: grip.dataset.co as string }
      drag.current = { mode: 'move', sx: e.clientX, sy: e.clientY, from: { x: 0, y: 0 }, name: '', moved: false, additive: false, grip: store.gripBox(ref) }
    } else {
      drag.current = {
        mode: e.shiftKey ? 'box' : 'pan', sx: e.clientX, sy: e.clientY, from: { x: s.view.x, y: s.view.y },
        name: '', moved: false, additive: e.shiftKey,
      }
    }
  }

  const onMove = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (!d) return
    const dx = e.clientX - d.sx
    const dy = e.clientY - d.sy
    if (!d.moved && Math.hypot(dx, dy) < DRAG_SLOP) return
    if (!d.moved) {
      /* Captured only once it is a drag: a capture from the press would send the click and the
         double click to the stage instead of the card they were on. */
      stage.current?.setPointerCapture?.(e.pointerId)
    }
    d.moved = true
    if (d.mode === 'pan') {
      store.setView({ ...s.view, x: d.from.x + dx, y: d.from.y + dy })
    } else if (d.mode === 'card') {
      store.placeCard(d.name, { x: d.from.x + dx / s.view.k, y: d.from.y + dy / s.view.k })
      const p = toCanvas(e.clientX, e.clientY)
      setDropAt(store.cardUnder(p.x, p.y, d.name))
    } else if (d.mode === 'move' && d.grip) {
      store.moveBox(d.grip, dx / s.view.k, dy / s.view.k)
    } else {
      const a = toCanvas(d.sx, d.sy)
      const b = toCanvas(e.clientX, e.clientY)
      setBox({ x0: a.x, y0: a.y, x1: b.x, y1: b.y })
    }
  }

  const onUp = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    drag.current = null
    setBox(null)
    setDropAt(null)
    if (!d) return
    if (d.mode === 'card') {
      if (!d.moved) {
        store.select(d.name, d.additive)
        return
      }
      const p = toCanvas(e.clientX, e.clientY)
      const into = boxAt(store.layout().boxes, p.x, p.y)
      const row = store.rowOf(d.name)
      if (into) {
        void store.moveMember(d.name, into.company, into.dept)
        return
      }
      if (row?.company) {
        void store.moveMember(d.name, '', '')
        return
      }
      const target = store.cardUnder(p.x, p.y, d.name)
      if (target && !store.rowOf(target)?.company) {
        store.unplace(d.name)
        void store.reparent(d.name, target)
      }
    } else if (d.mode === 'move' && d.grip) {
      if (d.moved) store.moveBox(d.grip, (e.clientX - d.sx) / s.view.k, (e.clientY - d.sy) / s.view.k, true)
    } else if (d.mode === 'box') {
      if (d.moved && box) store.selectInBox(box.x0, box.y0, box.x1, box.y1, true)
    } else if (!d.moved) {
      store.clearSelection()
    }
  }

  const onContext = (e: ReactMouseEvent<HTMLDivElement>): void => {
    e.preventDefault()
    const rect = stage.current?.getBoundingClientRect()
    setMenu({
      x: e.clientX - (rect?.left ?? 0),
      y: e.clientY - (rect?.top ?? 0),
      at: toCanvas(e.clientX, e.clientY),
      target: targetOf(e.target as HTMLElement),
    })
  }

  const focusCard = (name: string): void => {
    store.focusOn(name)
    requestAnimationFrame(() => stage.current?.querySelector<HTMLElement>(`[data-card="${CSS.escape(name)}"]`)?.focus())
  }

  const onKey = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    const at = (e.target as HTMLElement).closest<HTMLElement>('[data-card]')?.dataset.card ?? null
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault()
      void store.undo()
    } else if (arrows.includes(e.key) && at) {
      e.preventDefault()
      const next = step(s.org, at, e.key as Arrow, order)
      store.select(next)
      focusCard(next)
    } else if (e.key === 'Enter' && at) {
      e.preventDefault()
      store.openInspector(at)
    } else if (e.key === 'Escape') {
      if (s.inspect || s.selected.length || s.edge) {
        e.stopPropagation()
        store.clearSelection()
      }
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && (at || s.edge)) {
      e.preventDefault()
      if (s.edge) {
        void store.unlinkSelected()
      } else if (at && s.org.some((r) => r.name === at)) {
        askRemove(at)
      }
    } else if (e.key === '+' || e.key === '=') {
      store.zoomBy(1.2)
    } else if (e.key === '-') {
      store.zoomBy(1 / 1.2)
    } else if (e.key === '0') {
      store.fit()
    } else if (e.key === 'Tab' && at) {
      /* Tab walks the tree in order instead of the page's own order. */
      const i = order.indexOf(at)
      const next = order[i + (e.shiftKey ? -1 : 1)]
      if (next) {
        e.preventDefault()
        store.select(next)
        focusCard(next)
      }
    }
  }

  const menuItems = (m: MenuAt): { title?: string; items: MenuItem[] } => {
    const target = m.target
    if (target.kind === 'company') {
      const c = store.companyOf(target.id)
      if (!c) return { items: [] }
      return {
        title: c.name,
        items: [
          { label: t('gui.canvas.m_add_dept'), run: () => setDialog({ kind: 'dept-new', id: c.id, at: m.at }) },
          { label: t('gui.canvas.m_add_employee'), run: () => setDialog({ kind: 'hire', id: c.id, dept: '' }) },
          { label: t('gui.canvas.m_set_company_head'), run: () => setDialog({ kind: 'head', id: c.id, dept: '' }) },
          { label: t('gui.canvas.m_rename'), run: () => setDialog({ kind: 'company-rename', id: c.id }) },
          { label: t('gui.canvas.m_logo'), run: () => setDialog({ kind: 'company-edit', id: c.id }) },
          { label: t('gui.canvas.m_background'), run: () => setDialog({ kind: 'bg-company', id: c.id }) },
          c.frozen
            ? { label: t('gui.canvas.m_unfreeze'), run: () => void store.updateCompany(c.id, { frozen: false }) }
            : { label: t('gui.canvas.m_freeze'), run: () => void store.updateCompany(c.id, { frozen: true }) },
          { label: t('gui.canvas.m_export_company'), run: () => setPanel(store.exportCompanyText(c.id)) },
          {
            label: t('gui.canvas.m_delete_company'), danger: true,
            run: () => confirm.ask(t('gui.canvas.m_delete_company'), t('gui.canvas.confirm_delete_company', { name: c.name }),
              t('gui.canvas.m_delete_company'), () => { void store.deleteCompany(c.id) }),
          },
        ],
      }
    }
    if (target.kind === 'dept') {
      return {
        title: target.dept,
        items: [
          { label: t('gui.canvas.m_add_dept_employee'), run: () => setDialog({ kind: 'hire', id: target.id, dept: target.dept }) },
          { label: t('gui.canvas.m_set_head'), run: () => setDialog({ kind: 'head', id: target.id, dept: target.dept }) },
          { label: t('gui.canvas.m_rename'), run: () => setDialog({ kind: 'dept-rename', id: target.id, dept: target.dept }) },
          { label: t('gui.canvas.m_background'), run: () => setDialog({ kind: 'bg-dept', id: target.id, dept: target.dept }) },
          {
            label: t('gui.canvas.m_delete_dept'), danger: true,
            run: () => confirm.ask(t('gui.canvas.m_delete_dept'), t('gui.canvas.confirm_delete_dept', { name: target.dept }),
              t('gui.canvas.m_delete_dept'), () => { void store.deleteDepartment(target.id, target.dept) }),
          },
        ],
      }
    }
    if (target.kind === 'card') {
      const name = target.name
      const row = store.rowOf(name)
      const company = row?.company ? store.companyOf(row.company) : undefined
      const items: MenuItem[] = [
        { label: t('gui.canvas.m_open'), run: () => store.openInspector(name) },
        { label: t('gui.canvas.m_avatar'), run: () => setDialog({ kind: 'avatar', name }) },
      ]
      if (store.isEmployee(name)) items.push({ label: t('gui.canvas.m_prompt'), run: () => store.openInspector(name, 'prompt') })
      if (company && row) {
        if (row.department) {
          const isHead = company.departments.find((d) => d.name === row.department)?.head === name
          if (!isHead) items.push({ label: t('gui.canvas.m_make_head'), run: () => void store.makeHead(name) })
          items.push({ label: t('gui.canvas.m_to_company_level'), run: () => void store.moveMember(name, company.id, '') })
        } else if (company.head !== name) {
          items.push({ label: t('gui.canvas.m_make_company_head'), run: () => void store.makeHead(name) })
        }
        items.push({ label: t('gui.canvas.m_leave_company'), run: () => void store.moveMember(name, '', '') })
      }
      if (store.isEmployee(name)) items.push({ label: t('gui.canvas.m_retire'), danger: true, run: () => askRetire(name) })
      else if (row) items.push({ label: t('gui.canvas.m_remove_card'), danger: true, run: () => askRemove(name) })
      return { title: name, items }
    }
    return {
      items: [
        { label: t('gui.canvas.m_add_company'), run: () => setDialog({ kind: 'company-new', at: m.at }) },
        { label: t('gui.canvas.m_add_employee'), run: () => setDialog({ kind: 'hire', id: '', dept: '' }) },
        { label: t('gui.canvas.m_fit'), run: () => store.fit() },
        { label: t('gui.canvas.m_layout'), run: () => store.autoLayout() },
      ],
    }
  }

  const notice = s.refused ? t(REFUSALS[s.refused])
    : s.error === 'import' ? t('gui.canvas.import_bad')
      : s.error === 'dept_taken' ? t('gui.canvas.dept_taken') : s.error

  const opened = menu ? menuItems(menu) : null

  return (
    <div className="canvas">
      <div className="canvas-bar" role="toolbar" aria-label={t('gui.canvas.tools')}>
        <div className="canvas-title">{t('gui.canvas.page')}</div>
        <span className="canvas-grow" />
        <button type="button" className="canvas-tool" onClick={() => setDialog({ kind: 'company-new', at: newCompanyPlace(boxes) })}>
          {t('gui.canvas.m_add_company')}
        </button>
        <button type="button" className="canvas-tool" onClick={() => store.zoomBy(1 / 1.2)} aria-label={t('gui.canvas.zoom_out')}>−</button>
        <span className="canvas-zoom" aria-live="off">{Math.round(s.view.k * 100)}%</span>
        <button type="button" className="canvas-tool" onClick={() => store.zoomBy(1.2)} aria-label={t('gui.canvas.zoom_in')}>+</button>
        <button type="button" className="canvas-tool" onClick={() => store.fit()}>{t('gui.canvas.fit')}</button>
        <button type="button" className="canvas-tool" onClick={() => store.autoLayout()}>{t('gui.canvas.layout')}</button>
        <button type="button" className="canvas-tool" disabled={!s.undo.length} onClick={() => void store.undo()}>{t('gui.canvas.undo')}</button>
        <button type="button" className="canvas-tool" aria-pressed={panel !== null} onClick={() => setPanel(panel === null ? store.exportText() : null)}>
          {t('gui.canvas.file')}
        </button>
      </div>
      {notice ? <div className="canvas-notice" role="alert">{notice}</div> : null}
      <div className="canvas-body">
        <div
          className="canvas-stage"
          ref={stage}
          role="tree"
          aria-label={t('gui.canvas.tree')}
          aria-multiselectable="true"
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onContextMenu={onContext}
          onKeyDown={onKey}
          data-grab={drag.current?.moved ? drag.current.mode : undefined}
        >
          <div className="canvas-layer" style={{ transform: `translate(${s.view.x}px, ${s.view.y}px) scale(${s.view.k})` }}>
            {s.companies.map((c) => <CompanyBox key={c.id} company={c} rect={boxes.companies.get(c.id)} count={membersOf(s.org, c.id).length} />)}
            {s.companies.flatMap((c) => c.departments.map((d) => {
              const rect = boxes.depts.get(deptKey(c.id, d.name))
              if (!rect) return null
              return (
                <div
                  key={deptKey(c.id, d.name)}
                  className="canvas-dept"
                  data-co={c.id}
                  data-dept={d.name}
                  data-frozen={c.frozen || undefined}
                  style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h, background: backdropCss(d.background, DEFAULT_DEPT_BG) }}
                >
                  <div className="canvas-dept-head" data-grip="" data-co={c.id} data-dept={d.name}>
                    <span className="canvas-dept-name">{d.name}</span>
                    <span className="canvas-dept-boss">{d.head ? `${t('gui.canvas.dept_head')}: ${d.head}` : t('gui.canvas.dept_no_head')}</span>
                  </div>
                </div>
              )
            }))}
            <svg className="canvas-wires" aria-hidden="true">
              <defs>
                <marker id="canvas-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
                  <path d="M0 0L10 5L0 10z" className="canvas-arrowhead" />
                </marker>
              </defs>
              {edges.map((edge) => {
                const key = `${edge.from}>${edge.to}`
                const d = wirePath(pos.get(edge.from) as Pos, pos.get(edge.to) as Pos)
                const hot = s.edge === key
                const dim = lit ? !(lit.has(edge.from) && lit.has(edge.to)) : false
                return (
                  <g key={key}>
                    <path className="canvas-edge" data-hot={hot || undefined} data-dim={dim || undefined} d={d} markerEnd="url(#canvas-arrow)" />
                    <path className="canvas-hit" data-wire={key} d={d} onClick={() => store.selectEdge(hot ? null : key)} />
                  </g>
                )
              })}
            </svg>
            {s.agents.map((a) => {
              const p = pos.get(a.name) as Pos
              const row = rows.get(a.name)
              const company = row?.company ? store.companyOf(row.company) : undefined
              const isHead = !!company && (company.head === a.name || company.departments.some((d) => d.name === row?.department && d.head === a.name))
              const heads = isHead || reportsOf(s.org, a.name).length > 0
              const kind = heads ? 'lead' : 'worker'
              const picked = s.selected.includes(a.name)
              const endOfEdge = !!s.edge && s.edge.split('>').includes(a.name)
              const face = row?.avatar ? avatarSrc(row.avatar) : null
              return (
                <div
                  key={a.name}
                  className="canvas-card"
                  role="treeitem"
                  aria-level={Math.max(1, levelOf(s.org, a.name))}
                  aria-selected={picked}
                  tabIndex={(s.focus ?? order[0]) === a.name ? 0 : -1}
                  data-card={a.name}
                  data-kind={kind}
                  data-sel={picked || undefined}
                  data-hot={endOfEdge || undefined}
                  data-dim={lit ? !lit.has(a.name) || undefined : undefined}
                  data-free={!row || undefined}
                  data-drop={dropAt === a.name || undefined}
                  data-frozen={company?.frozen || undefined}
                  style={{ left: p.x, top: p.y, width: CARD_W, minHeight: CARD_H }}
                  onDoubleClick={() => store.openInspector(a.name)}
                  onFocus={() => store.focusOn(a.name)}
                >
                  <div className="canvas-card-top">
                    {face ? <img className="canvas-face" src={face} alt="" /> : <span className="canvas-face canvas-face-letter" aria-hidden="true">{a.name[0]?.toUpperCase()}</span>}
                    <div className="canvas-card-id">
                      <div className="canvas-name" title={a.name}>{isHead ? <span className="canvas-crown" aria-hidden="true">♛ </span> : null}{a.name}</div>
                      <div className="canvas-role">{a.role || row?.title || t(`gui.canvas.origin_${a.origin}`)}</div>
                    </div>
                  </div>
                  {a.description ? <div className="canvas-desc">{a.description}</div> : null}
                  <div className="canvas-chips">
                    {a.model ? <span className="canvas-chip">{a.model}</span> : null}
                    {row?.department && !row.company ? <span className="canvas-chip">{row.department}</span> : null}
                  </div>
                  <div className="canvas-status">
                    <span className="canvas-dot" data-ok={(a.ready && !company?.frozen) || undefined} aria-hidden="true" />
                    {company?.frozen ? t('gui.canvas.co_frozen') : a.ready ? t('gui.canvas.ready') : t('gui.canvas.not_ready')}
                  </div>
                </div>
              )
            })}
            {box ? (
              <div
                className="canvas-box"
                style={{ left: Math.min(box.x0, box.x1), top: Math.min(box.y0, box.y1), width: Math.abs(box.x1 - box.x0), height: Math.abs(box.y1 - box.y0) }}
              />
            ) : null}
          </div>
          {s.loaded && !s.agents.length && !s.companies.length ? <div className="canvas-empty">{t('gui.canvas.hint_empty')}</div> : null}
          {s.loading && !s.loaded ? <div className="canvas-empty">{t('gui.canvas.loading')}</div> : null}
          {menu && opened ? <ContextMenu x={menu.x} y={menu.y} title={opened.title} items={opened.items} onClose={closeMenu} /> : null}
        </div>
        {s.inspect ? <Inspector name={s.inspect} known={known} /> : null}
        {panel !== null ? <FilePanel seed={panel} onClose={() => setPanel(null)} /> : null}
      </div>
      {dialog ? <Dialogs dialog={dialog} busy={s.busy} onClose={() => setDialog(null)} /> : null}
    </div>
  )
}

/* Where the toolbar's "Add company" puts a new box: right of everything there is. */
function newCompanyPlace(boxes: ReturnType<typeof store.layout>['boxes']): Pos {
  const { cards } = store.layout()
  let right = 0
  for (const p of cards.values()) right = Math.max(right, p.x + CARD_W)
  for (const r of boxes.companies.values()) right = Math.max(right, r.x + r.w)
  return { x: right + 120, y: 0 }
}

function CompanyBox({ company, rect, count }: { company: Company; rect: { x: number; y: number; w: number; h: number } | undefined; count: number }): JSX.Element | null {
  if (!rect) return null
  return (
    <div
      className="canvas-co"
      data-company={company.id}
      data-frozen={company.frozen || undefined}
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h, background: backdropCss(company.background, DEFAULT_COMPANY_BG) }}
    >
      <div className="canvas-co-head" data-grip="" data-co={company.id}>
        <img className="canvas-co-logo" src={company.logo || letterLogo(company.name)} alt="" draggable={false} />
        <div className="canvas-co-id">
          <div className="canvas-co-name">{company.name}</div>
          <div className="canvas-co-meta">
            {company.head ? `${t('gui.canvas.co_head')}: ${company.head} · ` : ''}{t('gui.canvas.co_members', { n: count })}
          </div>
        </div>
        {company.frozen ? <span className="canvas-co-badge">❄ {t('gui.canvas.co_frozen')}</span> : null}
      </div>
    </div>
  )
}

function Dialogs({ dialog, busy, onClose }: { dialog: DialogState; busy: boolean; onClose: () => void }): JSX.Element | null {
  const s = store.get()
  const done = (p: Promise<unknown>): void => { void p.then(onClose) }
  if (dialog.kind === 'company-new') {
    return (
      <CompanyDialog title={t('gui.canvas.d_company_new')} ok={t('gui.canvas.d_create')} onClose={onClose}
        onSubmit={(name, logo) => done(store.addCompany(name, logo, dialog.at))} />
    )
  }
  if (dialog.kind === 'company-edit' || dialog.kind === 'company-rename') {
    const c = store.companyOf(dialog.id)
    if (!c) return null
    if (dialog.kind === 'company-rename') {
      return <NameDialog title={t('gui.canvas.d_company_rename')} value={c.name} ok={t('gui.canvas.d_save')} onClose={onClose}
        onSubmit={(name) => done(store.updateCompany(c.id, { name }))} />
    }
    return (
      <CompanyDialog title={t('gui.canvas.d_company_edit')} name={c.name} logo={c.logo} ok={t('gui.canvas.d_save')} onClose={onClose}
        onSubmit={(name, logo) => done(store.updateCompany(c.id, { name, logo }))} />
    )
  }
  if (dialog.kind === 'dept-new') {
    return <NameDialog title={t('gui.canvas.d_dept_new')} ok={t('gui.canvas.d_create')} onClose={onClose}
      onSubmit={(name) => done(store.addDepartment(dialog.id, name, dialog.at))} />
  }
  if (dialog.kind === 'dept-rename') {
    return <NameDialog title={t('gui.canvas.d_dept_rename')} value={dialog.dept} ok={t('gui.canvas.d_save')} onClose={onClose}
      onSubmit={(name) => done(store.updateDepartment(dialog.id, dialog.dept, { name }))} />
  }
  if (dialog.kind === 'hire') {
    const c = dialog.id ? store.companyOf(dialog.id) : undefined
    const where = c ? (dialog.dept ? `${c.name} › ${dialog.dept}` : c.name) : t('gui.canvas.d_hire_nowhere')
    const submit = (f: HireForm): void => {
      done(store.hire({ name: f.name, role: f.role, system_prompt: f.prompt }, dialog.id, dialog.dept, f.avatar))
    }
    return <HireDialog where={t('gui.canvas.d_hire_where', { where })} busy={busy} onClose={onClose} onSubmit={submit} />
  }
  if (dialog.kind === 'avatar') {
    return <AvatarDialog value={store.rowOf(dialog.name)?.avatar ?? ''} onClose={onClose}
      onSubmit={(id) => done(store.setAvatar(dialog.name, id))} />
  }
  if (dialog.kind === 'bg-company') {
    return <BackdropDialog value={store.companyOf(dialog.id)?.background || DEFAULT_COMPANY_BG} onClose={onClose}
      onSubmit={(id) => done(store.updateCompany(dialog.id, { background: id }))} />
  }
  if (dialog.kind === 'bg-dept') {
    const d = store.companyOf(dialog.id)?.departments.find((x) => x.name === dialog.dept)
    return <BackdropDialog value={d?.background || DEFAULT_DEPT_BG} onClose={onClose}
      onSubmit={(id) => done(store.updateDepartment(dialog.id, dialog.dept, { background: id }))} />
  }
  const c = store.companyOf(dialog.id)
  const people = membersOf(s.org, dialog.id, dialog.dept).map((r) => r.name).sort((a, b) => a.localeCompare(b))
  const now = dialog.dept ? c?.departments.find((d) => d.name === dialog.dept)?.head ?? '' : c?.head ?? ''
  return (
    <HeadDialog title={dialog.dept ? t('gui.canvas.m_set_head') : t('gui.canvas.m_set_company_head')} people={people} value={now} onClose={onClose}
      onSubmit={(name) => done(store.makeHead(name))} />
  )
}

const TABS: ReadonlyArray<readonly [Tab, string]> = [
  ['main', 'gui.canvas.tab_main'],
  ['prompt', 'gui.canvas.tab_prompt'],
  ['links', 'gui.canvas.tab_links'],
]

function Inspector({ name, known }: { name: string; known: string[] }): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const agent = s.agents.find((a) => a.name === name)
  if (!agent) return null
  const face = s.org.find((r) => r.name === name)?.avatar
  const src = face ? avatarSrc(face) : null
  return (
    <aside className="canvas-pane" aria-label={t('gui.canvas.inspector')}>
      <div className="canvas-pane-head">
        <div className="canvas-pane-who">
          {src ? <img className="canvas-face canvas-face-big" src={src} alt="" /> : null}
          <div>
            <div className="canvas-pane-name">{agent.name}</div>
            <div className="canvas-pane-id">{t(`gui.canvas.origin_${agent.origin}`)}</div>
          </div>
        </div>
        <button type="button" className="canvas-tool" onClick={() => store.closeInspector()} aria-label={t('gui.canvas.close')}>×</button>
      </div>
      <div className="canvas-tabs" role="tablist">
        {TABS.map(([id, key]) => (
          <button key={id} type="button" role="tab" aria-selected={s.tab === id} className="canvas-tab" onClick={() => store.setTab(id)}>
            {t(key)}
          </button>
        ))}
      </div>
      <div className="canvas-pane-body" role="tabpanel">
        {s.tab === 'main' ? <MainTab name={name} known={known} /> : s.tab === 'prompt' ? <PromptTab name={name} /> : <LinksTab name={name} />}
      </div>
    </aside>
  )
}

function MainTab({ name, known }: { name: string; known: string[] }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const agent = s.agents.find((a) => a.name === name)
  const row = byName(s.org).get(name)
  const company = row?.company ? s.companies.find((c) => c.id === row.company) : undefined
  const options = managersFor(s.org, name, known)
  const [department, setDepartment] = useState(row?.department ?? '')
  const [title, setTitle] = useState(row?.title ?? '')
  useEffect(() => { setDepartment(row?.department ?? ''); setTitle(row?.title ?? '') }, [name, row?.department, row?.title])
  return (
    <>
      {agent?.role ? <p className="canvas-text">{agent.role}</p> : null}
      {agent?.description ? <p className="canvas-text canvas-faint">{agent.description}</p> : null}
      {agent?.model ? <div className="canvas-chips"><span className="canvas-chip">{agent.model}</span></div> : null}
      {company ? (
        <>
          <div className="canvas-field">
            <span>{t('gui.canvas.company')}</span>
            <span className="canvas-text">{company.name}{company.frozen ? ` · ${t('gui.canvas.co_frozen')}` : ''}</span>
          </div>
          <div className="canvas-field">
            <span>{t('gui.canvas.department')}</span>
            <span className="canvas-text">{row?.department || t('gui.canvas.company_level')}</span>
          </div>
        </>
      ) : (
        <>
          <label className="canvas-field">
            <span>{t('gui.canvas.manager')}</span>
            <select
              value={row?.reports_to ?? ''}
              onChange={(e) => void store.reparent(name, e.target.value || null)}
            >
              <option value="">{t('gui.canvas.no_manager')}</option>
              {row?.reports_to && !options.includes(row.reports_to) ? <option value={row.reports_to}>{row.reports_to}</option> : null}
              {options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </label>
          <label className="canvas-field">
            <span>{t('gui.canvas.department')}</span>
            <input value={department} onChange={(e) => setDepartment(e.target.value)} onBlur={() => { if (department !== (row?.department ?? '')) void store.editMeta(name, { department }) }} />
          </label>
        </>
      )}
      <label className="canvas-field">
        <span>{t('gui.canvas.title')}</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => { if (title !== (row?.title ?? '')) void store.editMeta(name, { title }) }} />
      </label>
      {row && !store.isEmployee(name) ? (
        <button type="button" className="canvas-tool" onClick={() => askRemove(name)}>
          {t('gui.canvas.remove')}
        </button>
      ) : null}
      {store.isEmployee(name) ? (
        <button type="button" className="canvas-tool" onClick={() => askRetire(name)}>
          {t('gui.canvas.m_retire')}
        </button>
      ) : null}
    </>
  )
}

function PromptTab({ name }: { name: string }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const p = s.prompt
  if (!store.isEmployee(name)) return <p className="canvas-text canvas-faint">{t('gui.canvas.prompt_none')}</p>
  const dirty = p.text !== p.saved
  return (
    <>
      <textarea
        className="canvas-prompt"
        value={p.text}
        disabled={p.loading}
        aria-label={t('gui.canvas.tab_prompt')}
        onChange={(e) => store.editPrompt(e.target.value)}
      />
      <button type="button" className="canvas-save" disabled={!dirty || p.loading} onClick={() => void store.savePrompt()}>
        {t('gui.canvas.save')}
      </button>
      <div className="canvas-hist-title">{t('gui.canvas.history')}</div>
      {p.history.length ? (
        <ul className="canvas-hist">
          {p.history.map((text, i) => (
            <li key={`${i}:${text.length}`}>
              <span className="canvas-hist-text">{text.slice(0, 90) || '—'}</span>
              <button type="button" className="canvas-tool" onClick={() => store.rollbackTo(i)}>{t('gui.canvas.rollback')}</button>
            </li>
          ))}
        </ul>
      ) : <p className="canvas-text canvas-faint">{t('gui.canvas.history_empty')}</p>}
    </>
  )
}

function LinksTab({ name }: { name: string }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const row = byName(s.org).get(name)
  if (row?.company) {
    const peers = talksWith(s.org, s.companies, name)
    return (
      <>
        <div className="canvas-hist-title">{t('gui.canvas.talks_with')}</div>
        {peers.length ? (
          <ul className="canvas-hist">
            {peers.map((p) => (
              <li key={p}><button type="button" className="canvas-link" onClick={() => store.openInspector(p, 'links')}>{p}</button></li>
            ))}
          </ul>
        ) : <p className="canvas-text canvas-faint">{t('gui.canvas.talks_none')}</p>}
        <p className="canvas-text canvas-faint">{t('gui.canvas.talks_rule')}</p>
      </>
    )
  }
  const manager = row?.reports_to ?? null
  const reports = reportsOf(s.org, name)
  const above = chain(s.org, name)
  return (
    <>
      <div className="canvas-hist-title">{t('gui.canvas.manager')}</div>
      {manager ? (
        <button type="button" className="canvas-link" onClick={() => store.openInspector(manager, 'links')}>{manager}</button>
      ) : <p className="canvas-text canvas-faint">{t('gui.canvas.no_manager')}</p>}
      {above.length > 1 ? <p className="canvas-text canvas-faint">{above.join(' › ')}</p> : null}
      <div className="canvas-hist-title">{t('gui.canvas.reports')}</div>
      {reports.length ? (
        <ul className="canvas-hist">
          {reports.map((r) => (
            <li key={r}>
              <button type="button" className="canvas-link" onClick={() => store.openInspector(r, 'links')}>{r}</button>
              <button type="button" className="canvas-tool" onClick={() => void store.reparent(r, null)}>{t('gui.canvas.unlink')}</button>
            </li>
          ))}
        </ul>
      ) : <p className="canvas-text canvas-faint">{t('gui.canvas.no_reports')}</p>}
    </>
  )
}

/* The chart as a file the reader can copy out or paste in: no download, which a desktop
   window handles unevenly. A company's own export opens here too. */
function FilePanel({ seed, onClose }: { seed: string; onClose: () => void }): JSX.Element {
  const [text, setText] = useState(seed)
  useEffect(() => setText(seed), [seed])
  return (
    <aside className="canvas-pane canvas-file" aria-label={t('gui.canvas.file')}>
      <div className="canvas-pane-head">
        <div className="canvas-pane-name">{t('gui.canvas.file')}</div>
        <button type="button" className="canvas-tool" onClick={onClose} aria-label={t('gui.canvas.close')}>×</button>
      </div>
      <p className="canvas-text canvas-faint">{t('gui.canvas.file_hint')}</p>
      <textarea className="canvas-prompt canvas-json" value={text} onChange={(e) => setText(e.target.value)} aria-label={t('gui.canvas.file')} spellCheck={false} />
      <div className="canvas-row">
        <button type="button" className="canvas-tool" onClick={() => setText(store.exportText())}>{t('gui.canvas.file_reset')}</button>
        <button type="button" className="canvas-save" onClick={() => void store.importText(text).then((ok) => { if (ok) onClose() })}>
          {t('gui.canvas.file_apply')}
        </button>
      </div>
    </aside>
  )
}
