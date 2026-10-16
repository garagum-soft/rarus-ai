/* The workflow editor's model: a playbook's steps as the editor holds them.
 *
 * Pure, so every edit can be tested without a page. A step comes off the wire
 * exactly as the playbook file writes it (playbooks.graph.get), typed ones
 * included -- forEach, repeat, branches, collect -- and goes back the same way
 * (playbooks.graph.save), so an edit never flattens a type into its copies.
 * Fields the editor has no control for (inputs, skills, mcps, a branches list)
 * ride along untouched in `rest`. The server expands and checks; this module
 * only shapes what is sent.
 */

export type StepKind = 'step' | 'forEach' | 'repeat' | 'branches'

export interface Draft {
  id: string
  subagent: string
  summary: string
  prompt: string
  dependsOn: string[]
  kind: StepKind
  /* forEach: one item per line. */
  items: string
  /* repeat: how many passes. */
  passes: number
  collect: boolean
  collectSummary: string
  collectPrompt: string
  /* What the file holds that the editor does not edit, written back as it was. */
  rest: Record<string, unknown>
  collectRest: Record<string, unknown>
}

type Raw = Record<string, unknown>

const EDITED = new Set([
  'id', 'subagent', 'agent', 'node_summary', 'nodeSummary', 'prompt_template', 'promptTemplate',
  'depends_on', 'dependsOn', 'forEach', 'for_each', 'repeat', 'collect',
])

const text = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v))

function pick(raw: Raw, ...names: string[]): unknown {
  for (const name of names) if (name in raw) return raw[name]
  return undefined
}

export function fromRaw(raw: Raw): Draft {
  const deps = pick(raw, 'dependsOn', 'depends_on')
  const each = pick(raw, 'forEach', 'for_each')
  const items = Array.isArray(each) ? each : each && typeof each === 'object' ? (each as Raw).items : undefined
  const collect = raw.collect && typeof raw.collect === 'object' ? (raw.collect as Raw) : null
  const kind: StepKind = items !== undefined ? 'forEach' : 'repeat' in raw ? 'repeat' : 'branches' in raw ? 'branches' : 'step'
  const rest: Raw = {}
  for (const [k, v] of Object.entries(raw)) if (!EDITED.has(k)) rest[k] = v
  const collectRest: Raw = {}
  if (collect) {
    for (const [k, v] of Object.entries(collect)) {
      if (!['node_summary', 'nodeSummary', 'prompt_template', 'promptTemplate'].includes(k)) collectRest[k] = v
    }
  }
  return {
    id: text(raw.id),
    subagent: text(pick(raw, 'subagent', 'agent')),
    summary: text(pick(raw, 'node_summary', 'nodeSummary')),
    prompt: text(pick(raw, 'prompt_template', 'promptTemplate')),
    dependsOn: Array.isArray(deps) ? deps.map(text) : deps ? [text(deps)] : [],
    kind,
    items: Array.isArray(items) ? items.map(text).join('\n') : '',
    passes: typeof raw.repeat === 'number' ? raw.repeat : 2,
    collect: collect !== null,
    collectSummary: collect ? text(pick(collect, 'node_summary', 'nodeSummary')) : '',
    collectPrompt: collect ? text(pick(collect, 'prompt_template', 'promptTemplate')) : '',
    rest,
    collectRest,
  }
}

export function toRaw(d: Draft): Raw {
  const out: Raw = { id: d.id.trim(), subagent: d.subagent.trim(), node_summary: d.summary, prompt_template: d.prompt }
  if (d.dependsOn.length) out.depends_on = [...d.dependsOn]
  Object.assign(out, d.rest)
  if (d.kind !== 'branches') delete out.branches
  if (d.kind === 'forEach') out.forEach = d.items.split('\n').map((s) => s.trim()).filter(Boolean)
  if (d.kind === 'repeat') out.repeat = d.passes
  if (d.collect && d.kind !== 'step') {
    out.collect = { ...d.collectRest, node_summary: d.collectSummary, prompt_template: d.collectPrompt || '{{ results }}' }
  }
  return out
}

/* A fresh step, its id the first `step-N` not taken. */
export function blankStep(drafts: Draft[], agent = 'Raven'): Draft {
  const taken = new Set(drafts.map((d) => d.id))
  let n = drafts.length + 1
  while (taken.has(`step-${n}`)) n++
  return fromRaw({ id: `step-${n}`, subagent: agent, node_summary: '', prompt_template: '' })
}

export function updateStep(drafts: Draft[], id: string, patch: Partial<Draft>): Draft[] {
  return drafts.map((d) => (d.id === id ? { ...d, ...patch } : d))
}

/* Renaming carries every step that waited on the old id over to the new one. */
export function renameStep(drafts: Draft[], from: string, to: string): Draft[] {
  return drafts.map((d) => ({
    ...(d.id === from ? { ...d, id: to } : d),
    dependsOn: d.dependsOn.map((x) => (x === from ? to : x)),
  }))
}

/* Removing a step takes it off every step that waited on it. */
export function removeStep(drafts: Draft[], id: string): Draft[] {
  return drafts.filter((d) => d.id !== id).map((d) => ({ ...d, dependsOn: d.dependsOn.filter((x) => x !== id) }))
}

export function toggleDep(drafts: Draft[], id: string, dep: string): Draft[] {
  return drafts.map((d) => {
    if (d.id !== id || dep === id) return d
    const has = d.dependsOn.includes(dep)
    return { ...d, dependsOn: has ? d.dependsOn.filter((x) => x !== dep) : [...d.dependsOn, dep] }
  })
}

/* One expanded step as the preview draws it. */
export interface Planned {
  id: string
  summary: string
  agent: string
  after: string[]
}

/* The expanded steps in waves: each wave waits only on earlier ones, so every
   step in a wave runs at once. Depth is the longest path to the step, which is
   when the graph runner can start it. A dependency on a step that is not in the
   graph is ignored here -- the server reports it among the findings. */
export function waves(expanded: Raw[]): Planned[][] {
  const steps: Planned[] = expanded.map((n) => {
    const deps = pick(n, 'depends_on', 'dependsOn')
    return {
      id: text(n.id),
      summary: text(pick(n, 'node_summary', 'nodeSummary')) || text(n.id),
      agent: text(pick(n, 'subagent', 'agent')),
      after: Array.isArray(deps) ? deps.map(text) : [],
    }
  })
  const by = new Map(steps.map((s) => [s.id, s]))
  const depth = new Map<string, number>()
  const walking = new Set<string>()
  const of = (id: string): number => {
    const known = depth.get(id)
    if (known !== undefined) return known
    const step = by.get(id)
    if (!step || walking.has(id)) return 0
    walking.add(id)
    const d = step.after.reduce((m, p) => (by.has(p) ? Math.max(m, of(p) + 1) : m), 0)
    walking.delete(id)
    depth.set(id, d)
    return d
  }
  const out: Planned[][] = []
  for (const step of steps) {
    const d = of(step.id)
    while (out.length <= d) out.push([])
    out[d]!.push(step)
  }
  return out
}