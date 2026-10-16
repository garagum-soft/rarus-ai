// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../../i18n/t'
import { workflowSource } from '../source'
import { Workflows } from './Workflows'

import type { PlaybookRow } from '../../../rpc/generated'

const row = (name: string, origin = 'user'): PlaybookRow =>
  ({ name, description: '', task_summary: '', schema_version: 1, artifact_kind: 'workflow', workers: [], mode: 'dag',
    confirm: false, origin, disabled: false, nodes: [], error: '' }) as PlaybookRow

const graph = {
  name: 'review-each', description: 'review every file', origin: 'builtin' as const,
  nodes: [{ id: 'review', subagent: 'Raven', node_summary: 'Review', prompt_template: 'Review {{ item }}', forEach: ['a', 'b'] }],
  expanded: [
    { id: 'review-1', subagent: 'Raven', node_summary: 'Review (1/2)', depends_on: [] },
    { id: 'review-2', subagent: 'Raven', node_summary: 'Review (2/2)', depends_on: [] },
  ],
  errors: [],
}

function wire(): void {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(workflowSource, 'list').mockResolvedValue([row('review-each', 'builtin'), row('draft-and-polish')])
  vi.spyOn(workflowSource, 'agents').mockResolvedValue(['Raven', 'Raven-Code'])
  vi.spyOn(workflowSource, 'get').mockResolvedValue(graph)
}

const settle = async (): Promise<void> => { await act(async () => { await Promise.resolve() }) }

afterEach(() => {
  cleanup()
  resetTranslator()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('the workflows section', () => {
  it('lists the graph workflows and opens one as its steps and the graph they run as', async () => {
    wire()
    vi.spyOn(workflowSource, 'check').mockResolvedValue({ expanded: graph.expanded, errors: [] })
    render(<Workflows />)
    await settle()

    expect([...document.querySelectorAll('.settings-wf-pb')].map((b) => b.textContent)).toEqual(['review-each', 'draft-and-polish'])
    fireEvent.click(screen.getByText('review-each'))
    await settle()

    expect(document.querySelector('.settings-wf-step')?.getAttribute('data-kind')).toBe('forEach')
    expect([...document.querySelectorAll('.settings-wf-step textarea')].map((el) => (el as HTMLTextAreaElement).value)).toContain('a\nb')
    expect(document.querySelector('.settings-wf-builtin')?.textContent).toBe('gui.settings.wf.builtin')
    expect([...document.querySelectorAll('.settings-wf-wave')].map((w) => [...w.querySelectorAll('[data-node]')].map((n) => n.getAttribute('data-node')))).toEqual([['review-1', 'review-2']])
    expect(document.querySelector('.settings-wf-ok')?.textContent).toBe('gui.settings.wf.no_errors')
  })

  it('checks the steps after a pause in typing and shows what is wrong', async () => {
    wire()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const check = vi.spyOn(workflowSource, 'check').mockResolvedValue({ expanded: [], errors: ['node b depends on ghost'] })
    render(<Workflows />)
    await settle()
    fireEvent.click(screen.getByText('gui.settings.wf.new'))
    await settle()

    expect(check).not.toHaveBeenCalled()
    await act(async () => { vi.advanceTimersByTime(450) })
    await settle()

    expect(check).toHaveBeenCalledTimes(1)
    expect(check.mock.calls[0]?.[0]).toEqual([{ id: 'step-1', subagent: 'Raven', node_summary: '', prompt_template: '' }])
    expect(document.querySelector('.settings-wf-errors li')?.textContent).toBe('node b depends on ghost')
    expect((document.querySelector('.settings-wf-save') as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves the steps as written, over the opened workflow, and says where they went', async () => {
    wire()
    vi.spyOn(workflowSource, 'check').mockResolvedValue({ expanded: graph.expanded, errors: [] })
    const save = vi.spyOn(workflowSource, 'save').mockResolvedValue({
      name: 'review-each', path: 'playbooks/review-each/playbook.md', origin: 'user', shadows_builtin: true,
      expanded: graph.expanded, errors: [],
    })
    render(<Workflows />)
    await settle()
    fireEvent.click(screen.getByText('review-each'))
    await settle()

    fireEvent.click(screen.getByText('gui.settings.wf.save'))
    await settle()

    expect(save).toHaveBeenCalledWith('review-each', [graph.nodes[0]], 'review every file', true)
    expect(document.querySelector('.settings-wf-note')?.textContent).toBe(
      'gui.settings.wf.saved_shadow {"path":"playbooks/review-each/playbook.md"}',
    )
  })
})
