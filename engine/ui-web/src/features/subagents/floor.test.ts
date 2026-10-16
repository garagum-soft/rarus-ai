import { describe, expect, it } from 'vitest'

import { activityOf, avatarHue, columnsFor, initials, layoutOffice, poseAt, spriteFor, stalledFor } from './floor'

import type { InstanceRow, SubagentRow } from './types'

const agent = (name: string, over: Partial<SubagentRow> = {}): SubagentRow =>
  ({ name, kind: 'cli', description: '', enabled: true, configured: true, ...over }) as SubagentRow

const inst = (agentName: string, status: string, handle = 'h'): InstanceRow =>
  ({ sessionKey: 's', agent: agentName, handle, kind: 'cli', status }) as InstanceRow

describe('the office floor plan', () => {
  it('gives every roster agent a desk on a square-ish grid, in roster order', () => {
    const desks = layoutOffice(['a', 'b', 'c', 'd', 'e'].map((n) => agent(n)), [])

    expect(desks.map((d) => [d.name, d.col, d.row])).toEqual([
      ['a', 0, 0], ['b', 1, 0], ['c', 2, 0], ['d', 0, 1], ['e', 1, 1],
    ])
  })

  it('keeps a desk for an agent the roster no longer lists while it still has work', () => {
    const desks = layoutOffice([agent('a')], [inst('ghost', 'running')])

    expect(desks.map((d) => d.name)).toEqual(['a', 'ghost'])
    expect(desks[1]?.activity).toBe('working')
  })

  it('never grows wider than the cap', () => {
    expect(columnsFor(1)).toBe(1)
    expect(columnsFor(4)).toBe(2)
    expect(columnsFor(30)).toBe(4)
  })
})

describe('what an agent is doing', () => {
  it('is working while any instance is mid-turn, and counts them', () => {
    const desks = layoutOffice([agent('a'), agent('b')], [inst('a', 'running', 'x'), inst('a', 'idle', 'y'), inst('a', 'run', 'z')])

    expect(desks[0]?.activity).toBe('working')
    expect(desks[0]?.busy).toBe(2)
    expect(desks[1]?.activity).toBe('idle')
  })

  it('is off when the roster has it switched off and nothing runs', () => {
    expect(activityOf(agent('a', { enabled: false }), [])).toBe('off')
    expect(activityOf(agent('a', { enabled: false }), [inst('a', 'running')])).toBe('working')
    expect(activityOf(agent('a'), [inst('a', 'done')])).toBe('idle')
  })
})

describe('the avatar', () => {
  it('keeps one colour per name and draws initials', () => {
    expect(avatarHue('hermes')).toBe(avatarHue('hermes'))
    expect(avatarHue('hermes')).not.toBe(avatarHue('codex'))
    expect(initials('research-raven')).toBe('RR')
    expect(initials('Codex')).toBe('CO')
    expect(initials('***')).toBe('?')
  })
})

describe('the portrait', () => {
  it('is the raven for the kind of work the name says, and the main raven otherwise', () => {
    expect(spriteFor('Raven-Code')).toBe('coding')
    expect(spriteFor('research-raven')).toBe('research')
    expect(spriteFor('Raven-Research')).toBe('research')
    expect(spriteFor('content-raven')).toBe('content')
    expect(spriteFor('Raven-PPT')).toBe('content')
    expect(spriteFor('data-raven')).toBe('data')
    expect(spriteFor('Raven')).toBe('main-agent')
    expect(spriteFor('hermes')).toBe('main-agent')
  })

  it('rides on every desk', () => {
    expect(layoutOffice([agent('Raven-Code'), agent('hermes')], []).map((d) => d.sprite)).toEqual(['coding', 'main-agent'])
  })
})

describe('an agent with no sign of life', () => {
  const NOW = 1_700_001_000_000
  const at = (agentName: string, quietMs: number | undefined, handle = 'h'): InstanceRow =>
    ({ ...inst(agentName, 'running', handle), lastSignalAtMs: quietMs === undefined ? undefined : NOW - quietMs }) as InstanceRow

  it('is marked once every running turn has been quiet ten minutes', () => {
    expect(stalledFor([at('a', 700_000)], NOW)).toBe(700_000)
    expect(stalledFor([at('a', 700_000, 'x'), at('a', 900_000, 'y')], NOW)).toBe(700_000)
  })

  it('is not marked while any turn shows life, nor when the server says nothing', () => {
    expect(stalledFor([at('a', 700_000, 'x'), at('a', 5_000, 'y')], NOW)).toBe(0)
    expect(stalledFor([at('a', 120_000)], NOW)).toBe(0)
    expect(stalledFor([at('a', undefined)], NOW)).toBe(0)
    expect(stalledFor([inst('a', 'idle')], NOW)).toBe(0)
  })

  it('reaches the desk', () => {
    const [desk] = layoutOffice([agent('a')], [at('a', 660_000)], 4, NOW)

    expect(desk?.activity).toBe('working')
    expect(desk?.stalledMs).toBe(660_000)
  })
})

describe('the animation', () => {
  it('moves a working agent: its hands and its screen change over time', () => {
    const a = poseAt('working', 0)
    const b = poseAt('working', 100)

    expect(a.typing).not.toBe(b.typing)
    expect(b.glow).toBeGreaterThan(0.5)
  })

  it('lets an idle agent doze with a dim screen, and keeps an off agent still', () => {
    expect(poseAt('idle', 0).glow).toBeLessThan(0.5)
    expect([poseAt('idle', 0).doze, poseAt('idle', 1300).doze]).toEqual([true, false])
    expect(poseAt('idle', 0).typing).toBe(0)
    expect(poseAt('off', 5000)).toEqual({ bob: 0, typing: 0, glow: 0, doze: false })
  })

  it('switches an agent to the working animation when its instance starts a turn', () => {
    const before = layoutOffice([agent('a')], [inst('a', 'idle')])[0]?.activity ?? 'off'
    const after = layoutOffice([agent('a')], [inst('a', 'running')])[0]?.activity ?? 'off'

    expect(before).toBe('idle')
    expect(poseAt(before, 500).typing).toBe(0)
    expect(poseAt(after, 500).typing).not.toBe(0)
  })
})
