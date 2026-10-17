import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import * as lang from '../../state/lang'
import * as page from '../../state/page'
import { CARD_H, CARD_W, byName, chain, edgesOf, levelOf, managersFor, reportsOf, step, wirePath } from './graph'
import * as store from './store'

import type { Arrow, Pos } from './graph'
import type { Tab } from './store'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import './styles.css'

/* The agent hierarchy as a graph: the orchestrator on top, its agents below, a link from
   each manager to each report. Pan by dragging empty space, zoom with the wheel, drag a card
   onto another to make it report there, Shift-drag empty space to pick several. Everything
   the mouse does has a key: Tab into the tree, arrows along it, Enter for the inspector,
   Delete to take a card out, Esc to let go, Ctrl+Z to undo. */

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

interface Drag {
  mode: 'pan' | 'card' | 'box'
  /* Pointer at the start, in screen pixels; the card or view it moved from. */
  sx: number
  sy: number
  from: Pos
  name: string
  moved: boolean
  additive: boolean
}

/* Nothing is drawn while the page is shut: the island is mounted at boot with the rest, and a
   canvas painted into a closed page would cost every first paint a tree nobody can see. */
export function CanvasApp(): JSX.Element | null {
  useSyncExternalStore(lang.subscribe, lang.get)
  const shown = useSyncExternalStore(page.subscribe, page.get)
  return shown === 'canvasPage' ? <CanvasBody /> : null
}

function CanvasBody(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const [panel, setPanel] = useState(false)
  const stage = useRef<HTMLDivElement>(null)
  const drag = useRef<Drag | null>(null)
  const [box, setBox] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [dropAt, setDropAt] = useState<string | null>(null)

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

  const pos = store.positions()
  const lit = store.lit()
  const order = store.order()
  const edges = edgesOf(s.org).filter((e) => pos.has(e.from) && pos.has(e.to))
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
    const target = (e.target as HTMLElement).closest<HTMLElement>('[data-card]')
    const wire = (e.target as HTMLElement).closest<HTMLElement>('[data-wire]')
    if (wire) return
    if (target) {
      const name = target.dataset.card as string
      const at = pos.get(name) as Pos
      drag.current = { mode: 'card', sx: e.clientX, sy: e.clientY, from: at, name, moved: false, additive: e.shiftKey }
    } else {
      drag.current = {
        mode: e.shiftKey ? 'box' : 'pan', sx: e.clientX, sy: e.clientY, from: { x: s.view.x, y: s.view.y },
        name: '', moved: false, additive: e.shiftKey,
      }
    }
    stage.current?.setPointerCapture?.(e.pointerId)
  }

  const onMove = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (!d) return
    const dx = e.clientX - d.sx
    const dy = e.clientY - d.sy
    if (!d.moved && Math.hypot(dx, dy) < DRAG_SLOP) return
    d.moved = true
    if (d.mode === 'pan') {
      store.setView({ ...s.view, x: d.from.x + dx, y: d.from.y + dy })
    } else if (d.mode === 'card') {
      store.placeCard(d.name, { x: d.from.x + dx / s.view.k, y: d.from.y + dy / s.view.k })
      const p = toCanvas(e.clientX, e.clientY)
      setDropAt(store.cardUnder(p.x, p.y, d.name))
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
      const target = store.cardUnder(p.x, p.y, d.name)
      if (target) {
        store.unplace(d.name)
        void store.reparent(d.name, target)
      }
    } else if (d.mode === 'box') {
      if (d.moved && box) store.selectInBox(box.x0, box.y0, box.x1, box.y1, true)
    } else if (!d.moved) {
      store.clearSelection()
    }
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

  const notice = s.refused ? t(REFUSALS[s.refused]) : s.error === 'import' ? t('gui.canvas.import_bad') : s.error

  return (
    <div className="canvas">
      <div className="canvas-bar" role="toolbar" aria-label={t('gui.canvas.tools')}>
        <div className="canvas-title">{t('gui.canvas.page')}</div>
        <span className="canvas-grow" />
        {notice ? <span className="canvas-notice" role="alert">{notice}</span> : null}
        <button type="button" className="canvas-tool" onClick={() => store.zoomBy(1 / 1.2)} aria-label={t('gui.canvas.zoom_out')}>−</button>
        <span className="canvas-zoom" aria-live="off">{Math.round(s.view.k * 100)}%</span>
        <button type="button" className="canvas-tool" onClick={() => store.zoomBy(1.2)} aria-label={t('gui.canvas.zoom_in')}>+</button>
        <button type="button" className="canvas-tool" onClick={() => store.fit()}>{t('gui.canvas.fit')}</button>
        <button type="button" className="canvas-tool" onClick={() => store.autoLayout()}>{t('gui.canvas.layout')}</button>
        <button type="button" className="canvas-tool" disabled={!s.undo.length} onClick={() => void store.undo()}>{t('gui.canvas.undo')}</button>
        <button type="button" className="canvas-tool" aria-pressed={panel} onClick={() => setPanel(!panel)}>{t('gui.canvas.file')}</button>
      </div>
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
          onKeyDown={onKey}
          data-grab={drag.current?.moved ? drag.current.mode : undefined}
        >
          <div className="canvas-layer" style={{ transform: `translate(${s.view.x}px, ${s.view.y}px) scale(${s.view.k})` }}>
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
              const inChart = s.org.some((r) => r.name === a.name)
              const heads = reportsOf(s.org, a.name).length > 0
              const kind = heads ? 'lead' : 'worker'
              const picked = s.selected.includes(a.name)
              const endOfEdge = !!s.edge && s.edge.split('>').includes(a.name)
              const row = byName(s.org).get(a.name)
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
                  data-free={!inChart || undefined}
                  data-drop={dropAt === a.name || undefined}
                  style={{ left: p.x, top: p.y, width: CARD_W, minHeight: CARD_H }}
                  onDoubleClick={() => store.openInspector(a.name)}
                  onFocus={() => store.focusOn(a.name)}
                >
                  <div className="canvas-name" title={a.name}>{a.name}</div>
                  <div className="canvas-role">{a.role || row?.title || t(`gui.canvas.origin_${a.origin}`)}</div>
                  {a.description ? <div className="canvas-desc">{a.description}</div> : null}
                  <div className="canvas-chips">
                    {a.model ? <span className="canvas-chip">{a.model}</span> : null}
                    {row?.department ? <span className="canvas-chip">{row.department}</span> : null}
                  </div>
                  <div className="canvas-status">
                    <span className="canvas-dot" data-ok={a.ready || undefined} aria-hidden="true" />
                    {a.ready ? t('gui.canvas.ready') : t('gui.canvas.not_ready')}
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
          {s.loaded && !s.agents.length ? <div className="canvas-empty">{t('gui.canvas.empty')}</div> : null}
          {s.loading && !s.loaded ? <div className="canvas-empty">{t('gui.canvas.loading')}</div> : null}
        </div>
        {s.inspect ? <Inspector name={s.inspect} known={known} /> : null}
        {panel ? <FilePanel onClose={() => setPanel(false)} /> : null}
      </div>
    </div>
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
  return (
    <aside className="canvas-pane" aria-label={t('gui.canvas.inspector')}>
      <div className="canvas-pane-head">
        <div>
          <div className="canvas-pane-name">{agent.name}</div>
          <div className="canvas-pane-id">{t(`gui.canvas.origin_${agent.origin}`)}</div>
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
  const options = managersFor(s.org, name, known)
  const [department, setDepartment] = useState(row?.department ?? '')
  const [title, setTitle] = useState(row?.title ?? '')
  useEffect(() => { setDepartment(row?.department ?? ''); setTitle(row?.title ?? '') }, [name, row?.department, row?.title])
  return (
    <>
      {agent?.role ? <p className="canvas-text">{agent.role}</p> : null}
      {agent?.description ? <p className="canvas-text canvas-faint">{agent.description}</p> : null}
      {agent?.model ? <div className="canvas-chips"><span className="canvas-chip">{agent.model}</span></div> : null}
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
      <label className="canvas-field">
        <span>{t('gui.canvas.title')}</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => { if (title !== (row?.title ?? '')) void store.editMeta(name, { title }) }} />
      </label>
      {row ? (
        <button type="button" className="canvas-tool" onClick={() => askRemove(name)}>
          {t('gui.canvas.remove')}
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
  const manager = byName(s.org).get(name)?.reports_to ?? null
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
   window handles unevenly. */
function FilePanel({ onClose }: { onClose: () => void }): JSX.Element {
  const [text, setText] = useState(() => store.exportText())
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
