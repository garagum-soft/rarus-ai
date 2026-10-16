/* The team the offline page shows: one employee on this Raven's keys, held in
 * memory, added to by a hire, edited and retired.
 *
 * The live server writes a folder agent per hire (raven/agent/subagent/hire.py)
 * and refuses a name already on the roster; this keeps the one refusal the form
 * most needs to show and nothing of the folders. A key sent with a hire is
 * dropped here as it is on the server: nothing answers with it.
 */

import { RpcError } from '../transport'

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Employee = ResultOf<'employees.list'>['employees'][number]

const SEED: Employee[] = [
  {
    name: 'Mira', folder: 'mira',
    role: 'Researches a question across the web and sources, and answers with what she found and where.',
    owns: 'finds out what is known about a question', provider: '', model: '', own_key: false, hired_at: 1_790_000_000,
  },
]

const slug = (name: string): string => name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'employee'

export interface EmployeesFixture {
  fixtures: Fixtures
}

export function createEmployees(env: FixtureEnv): EmployeesFixture {
  const team = new Map<string, { row: Employee; prompt: string }>(
    SEED.map((row) => [row.folder, { row: { ...row }, prompt: 'You are Mira, a careful researcher. Cite every source.' }]),
  )
  const find = (raw: unknown): { row: Employee; prompt: string } | undefined => {
    const name = String(raw || '')
    return [...team.values()].find((e) => e.row.name.toLowerCase() === name.toLowerCase() || e.row.folder === name)
  }
  return {
    fixtures: {
      'employees.list': () => ({
        employees: [...team.values()].map((e) => e.row),
        providers: ['openai', 'openrouter'],
        all_providers: ['anthropic', 'deepseek', 'gemini', 'openai', 'openrouter'],
      }),
      'employees.hire': (p) => {
        const q = p as { name: string; role: string; system_prompt: string; owns?: string; provider?: string; model?: string; api_key?: string }
        const name = String(q.name || '')
        if (find(name) || name.toLowerCase() === 'raven') {
          const detail = `an agent named '${name}' is already on the roster; choose another name`
          throw new RpcError(-32602, 'invalid_params', { detail })
        }
        const row: Employee = {
          name, folder: slug(name), role: String(q.role || ''), owns: q.owns || '', provider: q.provider || '',
          model: q.model || '', own_key: !!q.api_key, hired_at: env.now() / 1000,
        }
        team.set(row.folder, { row, prompt: String(q.system_prompt || '') })
        return { employee: row }
      },
      'employees.prompt_get': (p) => ({ prompt: find(String((p as { name?: string }).name || ''))?.prompt ?? '' }),
      'employees.prompt_set': (p) => {
        const q = p as { name: string; prompt: string }
        const e = find(q.name)
        if (e) e.prompt = String(q.prompt || '')
        return { ok: !!e }
      },
      'employees.retire': (p) => {
        const e = find(String((p as { name?: string }).name || ''))
        if (e) team.delete(e.row.folder)
        return { kept: e ? `employees/.retired/${e.row.folder}` : '' }
      },
    },
  }
}
