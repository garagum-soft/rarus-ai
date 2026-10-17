/* The canvas's rpc source: the roster, the hired employees and the chart.
 *
 * Each of the three is asked on its own and one that fails does not take the
 * others with it: a server without employees still has a roster to draw, and a
 * chart that cannot be read is an empty one, not an empty page. */

import { gateway } from '../../rpc/gateway'
import { mergeAgents } from './graph'

import type { CanvasSource } from './types'

const ignore = <T>(fallback: T) => (): T => fallback

export const canvasSource: CanvasSource = {
  load: async () => {
    const [roster, employees, org] = await Promise.all([
      gateway().call('subagents.list', { probe: false }).then((r) => r.rows ?? []).catch(ignore([])),
      gateway().call('employees.list', {}).then((r) => r.employees ?? []).catch(ignore([])),
      gateway().call('org.get', {}),
    ])
    const rows = org.members.map((m) => ({
      name: m.name,
      reports_to: m.reports_to ?? null,
      department: m.department ?? '',
      title: m.title ?? '',
    }))
    return {
      agents: mergeAgents(roster.filter((r) => r.enabled), employees, rows),
      org: rows,
      maxDepth: org.max_depth,
    }
  },
  saveOrg: (rows) =>
    gateway().call('org.set', { members: rows }).then((r) =>
      r.members.map((m) => ({
        name: m.name,
        reports_to: m.reports_to ?? null,
        department: m.department ?? '',
        title: m.title ?? '',
      })),
    ),
  promptGet: (name) => gateway().call('employees.prompt_get', { name }).then((r) => r.prompt),
  promptSet: (name, prompt) => gateway().call('employees.prompt_set', { name, prompt }).then((r) => !!r.ok),
}
