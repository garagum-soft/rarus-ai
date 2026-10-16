/* The agent office: where each roster agent sits and what it is doing.
 *
 * Pure functions, so the floor plan, an agent's state and its animation pose
 * can be tested without a canvas (happy-dom has none). `Office.tsx` draws what
 * these return. One desk per roster agent, laid out on a grid in roster order;
 * an agent is working while any of its instances is mid-turn, idle while it is
 * enabled with nothing running, and off while the roster has it disabled. */

import { STALLED_MS } from './TurnClock'

import type { InstanceRow, SubagentRow } from './types'

export type Activity = 'working' | 'idle' | 'off'

/* The raven portraits under assets/ravens/, one per kind of work. */
export type Sprite = 'coding' | 'research' | 'content' | 'data' | 'main-agent'

export interface Desk {
  name: string
  col: number
  row: number
  activity: Activity
  hue: number
  initials: string
  /* How many of the agent's instances are mid-turn, for the label. */
  busy: number
  sprite: Sprite
  /* How long the agent has shown no sign of life while working, in ms, once
     every turn it is on has been quiet past STALLED_MS; 0 otherwise. The same
     line the instance pane and the graph runner draw (TurnClock.tsx). */
  stalledMs: number
}

/* Which portrait an agent wears, read off its name: the roster's own naming
   (Raven-Code, research-raven, content-raven) says what it is for. Anything
   unrecognised is the main raven rather than a guess at a speciality. */
const SPRITE_RULES: Array<[RegExp, Sprite]> = [
  [/cod(e|ing|er)|dev|build|engineer|program/, 'coding'],
  [/research|search|analy|investig|scholar/, 'research'],
  [/data|sql|metric|stat/, 'data'],
  [/content|writ|doc|ppt|slide|design|edit|copy|blog/, 'content'],
]

export function spriteFor(name: string): Sprite {
  const low = name.toLowerCase()
  return SPRITE_RULES.find(([test]) => test.test(low))?.[1] ?? 'main-agent'
}

/* How long an agent's running turns have all been quiet -- the shortest quiet
   among them -- once every one is past the stall line; 0 while any shows life,
   or when the server publishes no sign (an older one): a guess would call
   every long turn stalled. */
export function stalledFor(own: InstanceRow[], now: number): number {
  const running = own.filter(isBusy)
  if (!running.length) return 0
  let least = Infinity
  for (const it of running) {
    if (typeof it.lastSignalAtMs !== 'number') return 0
    least = Math.min(least, now - it.lastSignalAtMs)
  }
  return least >= STALLED_MS ? least : 0
}

/* Instance statuses that mean a turn is under way. The server writes `running`
   for a direct turn and a spawn; `run` and `queued` are the run-row spellings
   the same panel already reads (store.agentDot). */
const BUSY = new Set(['running', 'run', 'queued', 'answering'])

export function isBusy(it: InstanceRow): boolean {
  return BUSY.has(String(it.status || '').toLowerCase())
}

export function activityOf(row: SubagentRow | undefined, own: InstanceRow[]): Activity {
  if (own.some(isBusy)) return 'working'
  if (row && row.enabled === false) return 'off'
  return 'idle'
}

/* A stable hue per name, so an agent keeps its colour across reloads. */
export function avatarHue(name: string): number {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return h % 360
}

export function initials(name: string): string {
  const words = name.replace(/[^A-Za-z0-9]+/g, ' ').trim().split(/\s+/).filter(Boolean)
  const [first = '', second = ''] = words
  if (!first) return '?'
  if (!second) return first.slice(0, 2).toUpperCase()
  return (first.charAt(0) + second.charAt(0)).toUpperCase()
}

/* Columns for `count` desks: a square-ish floor, never wider than `maxCols`. */
export function columnsFor(count: number, maxCols = 4): number {
  return Math.max(1, Math.min(maxCols, Math.ceil(Math.sqrt(Math.max(count, 1)))))
}

export function layoutOffice(
  roster: SubagentRow[],
  instances: InstanceRow[],
  maxCols = 4,
  now: number = Date.now(),
): Desk[] {
  const byAgent = new Map<string, InstanceRow[]>()
  for (const it of instances) {
    const list = byAgent.get(it.agent) || []
    list.push(it)
    byAgent.set(it.agent, list)
  }
  /* Every agent with a desk: the roster first, in its own order, then any
     instance whose agent the roster no longer lists -- it is still at work. */
  const names = [...roster.map((r) => r.name)]
  for (const name of byAgent.keys()) if (!names.includes(name)) names.push(name)
  const cols = columnsFor(names.length, maxCols)
  const rows = new Map(roster.map((r) => [r.name, r]))
  return names.map((name, i) => {
    const own = byAgent.get(name) || []
    return {
      name,
      col: i % cols,
      row: Math.floor(i / cols),
      activity: activityOf(rows.get(name), own),
      hue: avatarHue(name),
      initials: initials(name),
      busy: own.filter(isBusy).length,
      sprite: spriteFor(name),
      stalledMs: stalledFor(own, now),
    }
  })
}

export interface Pose {
  /* Vertical offset of the avatar, in pixels. */
  bob: number
  /* Which hand is down on the keyboard, -1..1. */
  typing: number
  /* Monitor brightness, 0..1. */
  glow: number
  /* Whether the idle "z" is drawn this frame. */
  doze: boolean
}

/* The animation frame for an activity at time `ms`: a working agent types fast
   with its screen lit, an idle one breathes slowly with its screen dim, and an
   agent that is off does not move at all. */
export function poseAt(activity: Activity, ms: number): Pose {
  if (activity === 'working') {
    return { bob: Math.sin(ms / 120) * 1.5, typing: Math.sin(ms / 70), glow: 0.75 + 0.25 * Math.sin(ms / 200), doze: false }
  }
  if (activity === 'idle') {
    return { bob: Math.sin(ms / 900) * 1, typing: 0, glow: 0.15, doze: Math.floor(ms / 1200) % 2 === 0 }
  }
  return { bob: 0, typing: 0, glow: 0, doze: false }
}
