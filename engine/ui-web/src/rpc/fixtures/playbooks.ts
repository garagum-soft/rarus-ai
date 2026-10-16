/* The playbook library the offline page edits: two graph workflows, held in
 * memory, written over by a save.
 *
 * The live server expands typed steps and checks them (raven/playbook/
 * node_types.py, graph_edit.py). This answers with the same shapes and a small
 * expansion of its own -- forEach, repeat and collect, the three the editor
 * offers -- so the editor can be looked at with no gateway behind it. It does
 * not pretend to be the validator: the one finding it reports is the one the
 * editor most needs to show, a step waiting on a step that is not there.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { JsonValue, PlaybookRow } from '../generated'

type Raw = { [k: string]: JsonValue }

interface Stored { description: string; origin: string; nodes: Raw[] }

const SEED: Record<string, Stored> = {
  'review-each-file': {
    description: 'Review every changed file on its own, then merge the reviews into one.',
    origin: 'builtin',
    nodes: [
      {
        id: 'review', subagent: 'Raven', node_summary: 'Review a file',
        prompt_template: 'Review {{ item }} for bugs and unclear code.',
        forEach: ['api.py', 'models.py', 'views.py'],
        collect: { node_summary: 'Merge the reviews', prompt_template: 'Merge these reviews into one list:\n{{ results }}' },
      },
      { id: 'report', subagent: 'Raven', node_summary: 'Write the report', prompt_template: 'Write it up:\n{{ review.output }}', depends_on: ['review'] },
    ],
  },
  'draft-and-polish': {
    description: 'Draft a text, then improve it over three passes.',
    origin: 'user',
    nodes: [
      { id: 'draft', subagent: 'Raven', node_summary: 'Draft', prompt_template: 'Draft the announcement.' },
      {
        id: 'polish', subagent: 'Raven', node_summary: 'Polish', depends_on: ['draft'], repeat: 3,
        prompt_template: 'Improve this text:\n{{ previous }}\n\nThe first draft:\n{{ draft.output }}',
      },
    ],
  },
}

const deps = (n: Raw): string[] => (Array.isArray(n.depends_on) ? (n.depends_on as string[]) : [])

function expand(nodes: Raw[]): { expanded: Raw[]; errors: string[] } {
  const out: Raw[] = []
  const exits = new Map<string, string[]>()
  for (const n of nodes) {
    const id = String(n.id || '')
    const base: Raw = {
      id, subagent: String(n.subagent ?? ''), node_summary: String(n.node_summary ?? ''),
      prompt_template: String(n.prompt_template ?? ''), depends_on: deps(n),
    }
    const made: Raw[] = []
    if (Array.isArray(n.forEach)) {
      ;(n.forEach as unknown[]).forEach((item, i) => made.push({
        ...base, id: `${id}-${i + 1}`, node_summary: `${String(n.node_summary || id)} (${i + 1}/${(n.forEach as unknown[]).length})`,
        prompt_template: String(n.prompt_template || '').replace(/\{\{\s*item\s*\}\}/g, String(item)),
      }))
    } else if (typeof n.repeat === 'number') {
      for (let k = 1; k <= n.repeat; k++) {
        made.push({
          ...base, id: `${id}-${k}`, node_summary: `${String(n.node_summary || id)} (pass ${k}/${n.repeat})`,
          depends_on: k > 1 ? [...deps(n), `${id}-${k - 1}`] : deps(n),
          prompt_template: String(n.prompt_template || '').replace(/\{\{\s*previous\s*\}\}/g, k > 1 ? `{{ ${id}-${k - 1}.output }}` : '(nothing yet: this is the first pass)'),
        })
      }
    } else {
      out.push(base)
      continue
    }
    const collect = n.collect && typeof n.collect === 'object' ? (n.collect as Raw) : null
    if (collect) {
      made.push({ ...base, ...collect, id, depends_on: made.map((m) => String(m.id)) })
      exits.set(id, [id])
    } else {
      exits.set(id, typeof n.repeat === 'number' ? [String(made[made.length - 1]!.id)] : made.map((m) => String(m.id)))
    }
    out.push(...made)
  }
  const ids = new Set(out.map((n) => String(n.id)))
  const errors: string[] = []
  for (const n of out) {
    n.depends_on = deps(n).flatMap((d) => exits.get(d) ?? [d])
    for (const d of deps(n)) if (!ids.has(d)) errors.push(`node ${String(n.id)!} depends on ${d}, which is not in the graph`)
  }
  return { expanded: out, errors }
}

function row(name: string, s: Stored): PlaybookRow {
  return {
    name, description: s.description, task_summary: s.description, schema_version: 1, artifact_kind: 'workflow',
    workers: [], mode: 'dag', confirm: false, origin: s.origin, disabled: false, coordinator: false, coordinator_brief: '',
    nodes: s.nodes.map((n) => ({ id: String(n.id), depends_on: deps(n) })), error: '',
  }
}

export interface PlaybooksFixture {
  fixtures: Fixtures
}

export function createPlaybooks(_env: FixtureEnv): PlaybooksFixture {
  const library = new Map(Object.entries(SEED).map(([k, v]) => [k, { ...v, nodes: v.nodes.map((n) => ({ ...n })) }]))
  return {
    fixtures: {
      'playbooks.list': () => ({ playbooks: [...library].map(([name, s]) => row(name, s)) }),
      'playbooks.graph.get': (p) => {
        const name = String((p as { name?: string }).name || '')
        const s = library.get(name)
        /* The server refuses a name it does not have; the page only ever asks
           for one it listed, so the answer here says so in the findings. */
        if (!s) return { name, description: '', origin: 'user' as const, nodes: [], expanded: [], errors: [`no playbook named ${name}`] }
        return { name, description: s.description, origin: s.origin as 'builtin' | 'user', nodes: s.nodes, ...expand(s.nodes) }
      },
      'playbooks.graph.check': (p) => expand(((p as { nodes?: Raw[] }).nodes) || []),
      'playbooks.graph.save': (p) => {
        const q = p as { name: string; nodes?: Raw[]; description?: string | null }
        const nodes = q.nodes || []
        const before = library.get(q.name)
        library.set(q.name, { description: q.description ?? before?.description ?? '', origin: 'user', nodes })
        return {
          name: String(q.name || ''), path: `playbooks/${q.name}/playbook.md`, origin: 'user' as const,
          shadows_builtin: before?.origin === 'builtin', ...expand(nodes),
        }
      },
    },
  }
}
