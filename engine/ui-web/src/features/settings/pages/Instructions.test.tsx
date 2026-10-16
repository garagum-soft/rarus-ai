// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as confirm from '../../../state/confirm'
import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'

import type { InstrFile, InstrOpen, InstrSaved } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

const files: InstrFile[] = [
  { key: 'agent_memory/profile/soul.md', kind: 'instruction', exists: true, size: 12, sha256: 's1', head: 1, revisions: 1 },
  { key: 'TOOLS.md', kind: 'instruction', exists: false, size: 0, sha256: 'e0', head: 0, revisions: 0 },
  { key: 'user_memory/profile/user.md', kind: 'memory', exists: true, size: 20, sha256: 'u1', head: 0, revisions: 0 },
]

const soul = (text: string, sha: string, head: number): InstrOpen => ({
  key: 'agent_memory/profile/soul.md',
  kind: 'instruction',
  text,
  sha256: sha,
  head,
  revisions: [
    ...(head > 1 ? [{ rev: head, at: 1_700_000_100, author: 'web', note: '', action: 'save', restored_from: null }] : []),
    { rev: 1, at: 1_700_000_000, author: 'baseline', note: 'the text before revisions were kept', action: 'save', restored_from: null },
  ],
})

/* A gateway in memory: what the editor saves is what it reads back. */
function backend(conflictOnce = false) {
  let current = soul('I am Raven.\n', 's1', 1)
  let refuse = conflictOnce
  const calls: Array<[string, unknown]> = []
  const over = {
    instrList: async () => { calls.push(['list', null]); return files },
    instrGet: async (key: string) => { calls.push(['get', key]); return current },
    instrRevision: async (_key: string, rev: number) => { calls.push(['revision', rev]); return rev === 1 ? 'I am Raven.\n' : current.text },
    instrSave: async (key: string, text: string, sha: string): Promise<InstrSaved> => {
      calls.push(['save', { key, text, sha }])
      if (refuse) {
        refuse = false
        current = soul('The agent rewrote this.\n', 's-agent', 2)
        return 'conflict'
      }
      if (sha !== current.sha256) return 'conflict'
      current = soul(text, `s${current.head + 1}`, current.head + 1)
      return { sha256: current.sha256, head: current.head, saved: true }
    },
    instrRollback: async (key: string, rev: number, sha: string): Promise<InstrSaved> => {
      calls.push(['rollback', { key, rev, sha }])
      current = soul('I am Raven.\n', 'back', current.head + 1)
      return { sha256: 'back', head: current.head, saved: true }
    },
  }
  return { calls, over }
}

const editor = (): HTMLTextAreaElement => screen.getByRole('textbox') as HTMLTextAreaElement

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

describe('instructions and memory page', () => {
  it('lists the instruction files and the memory files apart', async () => {
    const { over } = backend()
    install(undefined, over)
    await mount('instructions')

    expect(screen.getByText('gui.settings.instr.instructions')).toBeTruthy()
    expect(screen.getByText('gui.settings.instr.memory')).toBeTruthy()
    expect(screen.getByText('agent_memory/profile/soul.md')).toBeTruthy()
    expect(screen.getByText('user_memory/profile/user.md')).toBeTruthy()
    expect(screen.getAllByText('gui.settings.instr.open')).toHaveLength(3)
  })

  it('opens a file, saves the edit against the hash it opened, and shows the new version', async () => {
    const { calls, over } = backend()
    install(undefined, over)
    await mount('instructions')

    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.instr.open')[0]!) })
    expect(editor().value).toBe('I am Raven.\n')
    expect((screen.getByText('gui.settings.instr.save') as HTMLButtonElement).disabled).toBe(true)

    await act(async () => { fireEvent.input(editor(), { target: { value: 'I am Raven, and brief.\n' } }) })
    expect(screen.getByText('gui.settings.instr.unsaved')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.save')) })

    expect(calls.find(([m]) => m === 'save')).toEqual([
      'save', { key: 'agent_memory/profile/soul.md', text: 'I am Raven, and brief.\n', sha: 's1' },
    ])
    expect(editor().value).toBe('I am Raven, and brief.\n')
    expect(screen.queryByText('gui.settings.instr.unsaved')).toBeNull()
    expect(screen.getByText('#2')).toBeTruthy()
  })

  it('a file that moved on is not overwritten: the reader keeps the edit or takes the newer text', async () => {
    const { calls, over } = backend(true)
    install(undefined, over)
    await mount('instructions')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.instr.open')[0]!) })
    await act(async () => { fireEvent.input(editor(), { target: { value: 'my edit\n' } }) })

    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.save')) })
    expect(screen.getByText('gui.settings.instr.conflict')).toBeTruthy()
    expect(editor().value).toBe('my edit\n')

    /* Keep mine: the newer text becomes the base and the edit stays on top; the next save carries the newer hash. */
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.keep_mine')) })
    expect(screen.queryByText('gui.settings.instr.conflict')).toBeNull()
    expect(editor().value).toBe('my edit\n')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.save')) })
    const saves = calls.filter(([m]) => m === 'save').map(([, a]) => (a as { sha: string }).sha)
    expect(saves).toEqual(['s1', 's-agent'])
  })

  it('taking the newer text drops the edit', async () => {
    const { over } = backend(true)
    install(undefined, over)
    await mount('instructions')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.instr.open')[0]!) })
    await act(async () => { fireEvent.input(editor(), { target: { value: 'my edit\n' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.save')) })

    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.take_theirs')) })

    expect(editor().value).toBe('The agent rewrote this.\n')
    expect(screen.queryByText('gui.settings.instr.unsaved')).toBeNull()
  })

  it('a revision can be viewed and put back after asking', async () => {
    const { calls, over } = backend()
    install(undefined, over)
    await mount('instructions')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.instr.open')[0]!) })
    await act(async () => { fireEvent.input(editor(), { target: { value: 'worse\n' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.save')) })

    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.instr.view')[1]!) })
    expect(document.querySelector('.settings-instr-peek')?.textContent).toBe('I am Raven.\n')
    const asked: string[] = []
    vi.spyOn(confirm, 'ask').mockImplementation((title, _b, _l, fn) => { asked.push(title); fn() })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.instr.restore')) })

    expect(asked).toEqual(['gui.settings.instr.restore_title {"rev":1}'])
    expect(calls.find(([m]) => m === 'rollback')).toEqual([
      'rollback', { key: 'agent_memory/profile/soul.md', rev: 1, sha: 's2' },
    ])
    expect(editor().value).toBe('I am Raven.\n')
  })

  it('the draft survives a remount of the page', async () => {
    const { over } = backend()
    install(undefined, over)
    await mount('instructions')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.instr.open')[0]!) })
    await act(async () => { fireEvent.input(editor(), { target: { value: 'half typed' } }) })

    await act(async () => { store.set({ epoch: store.get().epoch + 1 }) })

    expect(editor().value).toBe('half typed')
    expect(screen.getByText('gui.settings.instr.unsaved')).toBeTruthy()
  })
})
