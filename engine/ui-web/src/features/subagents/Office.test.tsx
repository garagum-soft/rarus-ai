// @vitest-environment happy-dom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { fitted, Office, portraitUrl } from './Office'

import type { InstanceRow, SubagentRow } from './types'

const agent = (name: string, enabled = true): SubagentRow =>
  ({ name, kind: 'cli', description: '', enabled, configured: true }) as SubagentRow

const inst = (agentName: string, status: string): InstanceRow =>
  ({ sessionKey: 's', agent: agentName, handle: 'h', kind: 'cli', status }) as InstanceRow

function wire(): void {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
}

afterEach(() => {
  cleanup()
  resetTranslator()
})

describe('the office island', () => {
  it('seats every agent and says what each is doing', () => {
    wire()
    render(<Office roster={[agent('hermes'), agent('codex'), agent('old', false)]} instances={[inst('hermes', 'running')]} />)

    const office = document.querySelector('.subagents-office')
    expect(office?.getAttribute('data-working')).toBe('1')
    expect(document.querySelector('canvas.subagents-office-canvas')).not.toBeNull()
    const rows = [...document.querySelectorAll('.subagents-office-legend li')].map((li) => [
      li.getAttribute('data-activity'),
      li.textContent,
    ])
    expect(rows).toEqual([
      ['working', 'hermesgui.ws.office_working {"n":1}'],
      ['idle', 'codexgui.ws.office_idle'],
      ['off', 'oldgui.ws.office_off'],
    ])
  })

  it('follows an agent from idle to working on the next render', () => {
    wire()
    const view = render(<Office roster={[agent('hermes')]} instances={[inst('hermes', 'idle')]} />)
    expect(document.querySelector('.subagents-office-legend li')?.getAttribute('data-activity')).toBe('idle')

    view.rerender(<Office roster={[agent('hermes')]} instances={[inst('hermes', 'running')]} />)
    expect(document.querySelector('.subagents-office-legend li')?.getAttribute('data-activity')).toBe('working')
  })

  it('names each desk its portrait and says when a working agent has gone quiet', () => {
    wire()
    const quiet = { ...inst('Raven-Code', 'running'), lastSignalAtMs: Date.now() - 720_000 } as InstanceRow
    render(<Office roster={[agent('Raven-Code'), agent('hermes')]} instances={[quiet]} />)

    const [code, hermes] = [...document.querySelectorAll('.subagents-office-legend li')]
    expect(code?.getAttribute('data-sprite')).toBe('coding')
    expect(code?.getAttribute('data-stalled')).toBe('true')
    expect(code?.querySelector('.subagents-quiet.subagents-quiet-stalled')?.textContent).toMatch(/^gui\.ws\.instance_stalled \{"d":"12m0\ds"\}$/)
    expect(hermes?.getAttribute('data-sprite')).toBe('main-agent')
    expect(hermes?.hasAttribute('data-stalled')).toBe(false)
  })

  it('fits a portrait into its square without stretching it', () => {
    expect(fitted(627, 1037, 46)).toEqual({ w: 28, h: 46 })
    expect(fitted(1142, 805, 46)).toEqual({ w: 46, h: 32 })
    expect(fitted(0, 0, 46)).toEqual({ w: 0, h: 0 })
  })

  it('finds the portraits where the page ships them, stamped like every other asset', () => {
    expect(portraitUrl('coding')).toBe('assets/ravens/coding.webp')
    ;(window as { __ASSETV?: string }).__ASSETV = 'abc123'
    try {
      expect(portraitUrl('research')).toBe('assets/ravens/research.webp?v=abc123')
    } finally {
      delete (window as { __ASSETV?: string }).__ASSETV
    }
  })

  it('says so when there is nobody to seat', () => {
    wire()
    render(<Office roster={[]} instances={[]} />)
    expect(document.querySelector('.wsempty')?.textContent).toBe('gui.ws.office_empty')
  })
})
