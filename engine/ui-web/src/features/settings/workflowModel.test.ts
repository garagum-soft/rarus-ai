import { describe, expect, it } from 'vitest'

import { blankStep, fromRaw, removeStep, renameStep, toRaw, toggleDep, updateStep, waves } from './workflowModel'

const review = {
  id: 'review', subagent: 'Raven', node_summary: 'Review a file', prompt_template: 'Review {{ item }}',
  forEach: ['api.py', 'views.py'], skills: ['code-review'],
  collect: { node_summary: 'Merge', prompt_template: 'Merge {{ results }}', subagent: 'Raven-Code' },
}

describe('a step as the editor holds it', () => {
  it('reads every spelling the file may use and writes the same step back', () => {
    const d = fromRaw(review)

    expect(d.kind).toBe('forEach')
    expect(d.items).toBe('api.py\nviews.py')
    expect(d.collect).toBe(true)
    expect(d.collectPrompt).toBe('Merge {{ results }}')
    expect(toRaw(d)).toEqual({
      id: 'review', subagent: 'Raven', node_summary: 'Review a file', prompt_template: 'Review {{ item }}',
      skills: ['code-review'], forEach: ['api.py', 'views.py'],
      collect: { subagent: 'Raven-Code', node_summary: 'Merge', prompt_template: 'Merge {{ results }}' },
    })
    expect(fromRaw({ id: 'x', agent: 'A', nodeSummary: 's', promptTemplate: 'p', dependsOn: ['y'] })).toMatchObject({
      subagent: 'A', summary: 's', prompt: 'p', dependsOn: ['y'], kind: 'step',
    })
  })

  it('keeps what it has no control for, and drops what a changed kind no longer means', () => {
    const branches = fromRaw({ id: 'b', subagent: 'Raven', branches: [{ prompt_template: 'one' }, { prompt_template: 'two' }] })
    expect(branches.kind).toBe('branches')
    expect(toRaw(branches).branches).toHaveLength(2)

    const plain = toRaw({ ...fromRaw(review), kind: 'step' })
    expect(plain).not.toHaveProperty('forEach')
    expect(plain).not.toHaveProperty('collect')

    const repeated = toRaw({ ...fromRaw(review), kind: 'repeat', passes: 4, collect: false })
    expect(repeated.repeat).toBe(4)
    expect(repeated).not.toHaveProperty('forEach')
  })

  it('writes blank lines out of the items and a bare collect as the results', () => {
    const d = { ...fromRaw(review), items: 'a\n\n  b  \n', collectPrompt: '' }
    expect(toRaw(d).forEach).toEqual(['a', 'b'])
    expect((toRaw(d).collect as Record<string, unknown>).prompt_template).toBe('{{ results }}')
  })
})

describe('editing the steps', () => {
  const steps = [fromRaw({ id: 'a', subagent: 'R' }), fromRaw({ id: 'b', subagent: 'R', depends_on: ['a'] })]

  it('names a new step after the first free number', () => {
    expect(blankStep(steps).id).toBe('step-3')
    expect(blankStep([fromRaw({ id: 'step-1' })]).id).toBe('step-2')
  })

  it('carries a rename over to every step that waited on it, and a removal off them', () => {
    expect(renameStep(steps, 'a', 'first').map((s) => [s.id, s.dependsOn])).toEqual([['first', []], ['b', ['first']]])
    expect(removeStep(steps, 'a').map((s) => [s.id, s.dependsOn])).toEqual([['b', []]])
  })

  it('toggles a dependency, never on the step itself', () => {
    expect(toggleDep(steps, 'b', 'a')[1]?.dependsOn).toEqual([])
    expect(toggleDep(steps, 'a', 'b')[0]?.dependsOn).toEqual(['b'])
    expect(toggleDep(steps, 'a', 'a')[0]?.dependsOn).toEqual([])
    expect(updateStep(steps, 'a', { summary: 'S' })[0]?.summary).toBe('S')
  })
})

describe('the waves', () => {
  it('puts every step after the last wave it waits on, so a wave runs at once', () => {
    const plan = waves([
      { id: 'r-1', subagent: 'Raven', node_summary: 'Review (1/2)' },
      { id: 'r-2', subagent: 'Raven', node_summary: 'Review (2/2)' },
      { id: 'r', subagent: 'Raven', node_summary: 'Merge', depends_on: ['r-1', 'r-2'] },
      { id: 'out', subagent: 'Raven', depends_on: ['r', 'ghost'] },
    ])

    expect(plan.map((w) => w.map((s) => s.id))).toEqual([['r-1', 'r-2'], ['r'], ['out']])
    expect(plan[1]?.[0]).toEqual({ id: 'r', summary: 'Merge', agent: 'Raven', after: ['r-1', 'r-2'] })
    expect(plan[2]?.[0]?.summary).toBe('out')
  })

  it('draws nothing for nothing, and survives a cycle the server would refuse', () => {
    expect(waves([])).toEqual([])
    expect(waves([{ id: 'a', depends_on: ['b'] }, { id: 'b', depends_on: ['a'] }]).flat().map((s) => s.id).sort()).toEqual(['a', 'b'])
  })
})