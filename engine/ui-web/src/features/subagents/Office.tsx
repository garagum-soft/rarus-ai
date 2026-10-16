/* The agent office: every roster agent at a desk on one floor, animated by
 * what it is doing (floor.ts decides the plan, the state, the pose and the
 * portrait).
 *
 * Drawn on a plain <canvas> with requestAnimationFrame: the page carries no
 * scene library and an office of a dozen desks does not need one. Each agent
 * wears the raven portrait for its kind of work (assets/ravens/*.webp, the
 * files the page already ships), and the procedural head with its initials is
 * what is drawn until the portrait has loaded, or if it never does. The list
 * under the canvas says the same thing in words, so the state is readable
 * without the drawing and testable where there is no canvas. */

import { useEffect, useRef } from 'react'

import { t } from '../../i18n/t'
import { assetStamp } from '../../lib/assetStamp'
import { formatDuration } from '../../lib/duration'
import { layoutOffice, poseAt } from './floor'

import type { Desk, Sprite } from './floor'
import type { InstanceRow, SubagentRow } from './types'
import type { JSX } from 'react'

const CELL_W = 150
const CELL_H = 120
const PORTRAIT = 46

/* One image per portrait for the page's lifetime; `onReady` repaints a floor
   that is not animating (every agent off) once its pictures arrive. */
const portraits = new Map<Sprite, HTMLImageElement>()

/* A picture `w` x `h` scaled to fit a `box` square, its proportions kept. */
export function fitted(w: number, h: number, box: number): { w: number; h: number } {
  const scale = box / Math.max(w, h, 1)
  return { w: Math.round(w * scale), h: Math.round(h * scale) }
}

export function portraitUrl(sprite: Sprite): string {
  return `assets/ravens/${sprite}.webp${assetStamp()}`
}

function portrait(sprite: Sprite, onReady: () => void): HTMLImageElement | null {
  if (typeof Image === 'undefined') return null
  let img = portraits.get(sprite)
  if (!img) {
    img = new Image()
    img.decoding = 'async'
    img.src = portraitUrl(sprite)
    portraits.set(sprite, img)
  }
  if (!img.complete) img.addEventListener('load', onReady, { once: true })
  return img
}

function token(el: Element, name: string, fallback: string): string {
  const value = getComputedStyle(el).getPropertyValue(name).trim()
  return value || fallback
}

function drawHead(g: CanvasRenderingContext2D, desk: Desk, x: number, ay: number): void {
  g.fillStyle = `hsl(${desk.hue} 55% 55%)`
  g.beginPath()
  g.ellipse(x, ay + 26, 22, 12, 0, Math.PI, 0)
  g.fill()
  g.beginPath()
  g.arc(x, ay, 15, 0, Math.PI * 2)
  g.fill()
  g.fillStyle = '#fff'
  g.font = '600 11px system-ui, sans-serif'
  g.textAlign = 'center'
  g.textBaseline = 'middle'
  g.fillText(desk.initials, x, ay + 1)
}

function drawDesk(
  g: CanvasRenderingContext2D,
  desk: Desk,
  ms: number,
  ink: string,
  line: string,
  amber: string,
  img: HTMLImageElement | null,
): void {
  const pose = poseAt(desk.activity, ms)
  const x = desk.col * CELL_W + CELL_W / 2
  const y = desk.row * CELL_H + 18
  const off = desk.activity === 'off'
  g.globalAlpha = off ? 0.45 : 1

  /* The avatar behind the desk: the portrait when it has loaded, else the
     procedural head and shoulders in the agent's own colour. */
  const ay = y + 30 + pose.bob
  if (img && img.complete && img.naturalWidth > 0) {
    /* Fitted, not stretched: the portraits are taller or wider than square. */
    const { w, h } = fitted(img.naturalWidth, img.naturalHeight, PORTRAIT)
    g.drawImage(img, x - w / 2, ay + PORTRAIT / 2 - 2 - h, w, h)
  } else {
    drawHead(g, desk, x, ay)
  }

  /* The desk, the monitor on it, and the keyboard the hands rest on. */
  g.fillStyle = line
  g.fillRect(x - 46, y + 52, 92, 8)
  g.fillStyle = ink
  g.fillRect(x - 3, y + 44, 6, 8)
  g.fillStyle = `rgba(120, 190, 255, ${0.15 + 0.85 * pose.glow})`
  g.fillRect(x + 18, y + 26, 26, 18)
  g.strokeStyle = ink
  g.strokeRect(x + 18, y + 26, 26, 18)
  g.fillStyle = `hsl(${desk.hue} 45% 45%)`
  const lift = pose.typing * 3
  g.fillRect(x - 16, y + 47 - Math.max(0, lift), 8, 4)
  g.fillRect(x + 8, y + 47 - Math.max(0, -lift), 8, 4)

  g.textAlign = 'center'
  g.textBaseline = 'middle'
  if (desk.stalledMs > 0) {
    /* Working, and no sign of life for ten minutes: a mark over the head,
       still at the desk -- it has not failed. */
    g.fillStyle = amber
    g.beginPath()
    g.arc(x + 22, y + 4, 8, 0, Math.PI * 2)
    g.fill()
    g.fillStyle = '#fff'
    g.font = '700 11px system-ui, sans-serif'
    g.fillText('…', x + 22, y + 3)
  } else if (pose.doze) {
    g.fillStyle = ink
    g.font = 'italic 12px system-ui, sans-serif'
    g.fillText('z', x + 20, y + 10)
  }

  g.globalAlpha = 1
  g.fillStyle = ink
  g.font = '12px system-ui, sans-serif'
  g.fillText(desk.name.length > 18 ? `${desk.name.slice(0, 17)}…` : desk.name, x, y + 76)
}

export function OfficeFloor({ desks }: { desks: Desk[] }): JSX.Element {
  const canvas = useRef<HTMLCanvasElement>(null)
  const cols = Math.max(1, ...desks.map((d) => d.col + 1))
  const rows = Math.max(1, ...desks.map((d) => d.row + 1))
  const animate = desks.some((d) => d.activity !== 'off')
  useEffect(() => {
    const el = canvas.current
    const g = el?.getContext?.('2d')
    if (!el || !g) return
    const ratio = window.devicePixelRatio || 1
    el.width = cols * CELL_W * ratio
    el.height = rows * CELL_H * ratio
    g.setTransform(ratio, 0, 0, ratio, 0, 0)
    const ink = token(el, '--text', '#1f1f1f')
    const line = token(el, '--line', '#ccc')
    const amber = token(el, '--amber', '#c98a1a')
    let frame = 0
    let live = true
    const images = new Map<Sprite, HTMLImageElement | null>()
    const paint = (ms: number): void => {
      g.clearRect(0, 0, cols * CELL_W, rows * CELL_H)
      for (const desk of desks) drawDesk(g, desk, ms, ink, line, amber, images.get(desk.sprite) ?? null)
      if (animate) frame = requestAnimationFrame(paint)
    }
    const repaint = (): void => {
      if (live && !animate) frame = requestAnimationFrame(paint)
    }
    for (const desk of desks) if (!images.has(desk.sprite)) images.set(desk.sprite, portrait(desk.sprite, repaint))
    frame = requestAnimationFrame(paint)
    return () => {
      live = false
      cancelAnimationFrame(frame)
    }
  }, [desks, cols, rows, animate])
  return (
    <canvas
      ref={canvas}
      className="subagents-office-canvas"
      style={{ width: cols * CELL_W, height: rows * CELL_H }}
      role="img"
      aria-label={t('gui.ws.office_view')}
    />
  )
}

export function Office({ roster, instances }: { roster: SubagentRow[]; instances: InstanceRow[] }): JSX.Element {
  const desks = layoutOffice(roster, instances)
  if (!desks.length) {
    return (
      <div className="wsempty">
        <div className="ttl">{t('gui.ws.office_empty')}</div>
      </div>
    )
  }
  return (
    <div className="subagents-office" data-working={desks.filter((d) => d.activity === 'working').length}>
      <OfficeFloor desks={desks} />
      <ul className="subagents-office-legend">
        {desks.map((d) => (
          <li key={d.name} data-activity={d.activity} data-sprite={d.sprite} data-stalled={d.stalledMs > 0 || undefined}>
            <b>{d.name}</b>
            <span>
              {d.activity === 'working'
                ? t('gui.ws.office_working', { n: d.busy })
                : t(d.activity === 'off' ? 'gui.ws.office_off' : 'gui.ws.office_idle')}
            </span>
            {d.stalledMs > 0 ? (
              <span className="subagents-quiet subagents-quiet-stalled">{t('gui.ws.instance_stalled', { d: formatDuration(d.stalledMs) })}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  )
}
