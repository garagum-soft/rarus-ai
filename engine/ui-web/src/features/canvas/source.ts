/* The canvas's rpc source: the roster, the hired employees, the chart and its companies.
 *
 * Each of the three is asked on its own and one that fails does not take the
 * others with it: a server without employees still has a roster to draw, and a
 * chart that cannot be read is an empty one, not an empty page. */

import { gateway } from '../../rpc/gateway'
import { mergeAgents } from './graph'

import type { OrgRow } from './graph'
import type { Company } from './org'
import type { CanvasSource } from './types'

const ignore = <T>(fallback: T) => (): T => fallback

interface WireMember { name: string; reports_to?: string | null; department?: string; title?: string; company?: string; avatar?: string }
interface WireCompany {
  id: string; name: string; logo?: string; head?: string; frozen?: boolean; background?: string
  departments?: Array<{ name: string; head?: string; background?: string }>
}

const rowOf = (m: WireMember): OrgRow => ({
  name: m.name,
  reports_to: m.reports_to ?? null,
  department: m.department ?? '',
  title: m.title ?? '',
  company: m.company ?? '',
  avatar: m.avatar ?? '',
})

const companyOf = (c: WireCompany): Company => ({
  id: c.id,
  name: c.name,
  logo: c.logo ?? '',
  head: c.head ?? '',
  frozen: !!c.frozen,
  background: c.background ?? '',
  departments: (c.departments ?? []).map((d) => ({ name: d.name, head: d.head ?? '', background: d.background ?? '' })),
})

export const canvasSource: CanvasSource = {
  load: async () => {
    const [roster, employees, org] = await Promise.all([
      gateway().call('subagents.list', { probe: false }).then((r) => r.rows ?? []).catch(ignore([])),
      gateway().call('employees.list', {}).then((r) => r.employees ?? []).catch(ignore([])),
      gateway().call('org.get', {}),
    ])
    const rows = org.members.map(rowOf)
    return {
      agents: mergeAgents(roster.filter((r) => r.enabled), employees, rows),
      org: rows,
      companies: (org.companies ?? []).map(companyOf),
      maxDepth: org.max_depth,
    }
  },
  saveOrg: (rows, companies) =>
    gateway().call('org.set', { members: rows, companies }).then((r) => ({
      org: r.members.map(rowOf),
      companies: (r.companies ?? []).map(companyOf),
    })),
  promptGet: (name) => gateway().call('employees.prompt_get', { name }).then((r) => r.prompt),
  promptSet: (name, prompt) => gateway().call('employees.prompt_set', { name, prompt }).then((r) => !!r.ok),
  hire: (spec) =>
    gateway().call('employees.hire', spec).then((r) => ({
      name: r.employee.name,
      origin: 'employee' as const,
      role: r.employee.role ?? '',
      description: r.employee.owns ?? '',
      model: r.employee.model ?? '',
      ready: true,
    })),
  retire: (name) => gateway().call('employees.retire', { name }).then(() => undefined),
}
