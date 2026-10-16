// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../../i18n/t'
import { teamSource } from '../source'
import { Team } from './Team'

import type { ResultOf } from '../../../rpc/generated'

type Employee = ResultOf<'employees.list'>['employees'][number]

const mira: Employee = {
  name: 'Mira', folder: 'mira', role: 'Researches questions.', owns: '', provider: '', model: '', own_key: false, hired_at: 1,
}

function wire(rows: Employee[] = [mira]): void {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(teamSource, 'list').mockResolvedValue({ employees: rows, providers: ['openai'], all_providers: ['anthropic', 'openai'] })
}

const settle = async (): Promise<void> => { await act(async () => { await Promise.resolve() }) }
const field = (label: string): HTMLInputElement =>
  screen.getByText(label).parentElement!.querySelector('input, textarea, select') as HTMLInputElement

afterEach(() => {
  cleanup()
  resetTranslator()
  vi.restoreAllMocks()
})

describe('the team section', () => {
  it('lists the employees with their model and whose keys they run on', async () => {
    wire()
    render(<Team />)
    await settle()

    const card = document.querySelector('[data-employee="mira"]')!
    expect(card.textContent).toContain('Mira')
    expect([...card.querySelectorAll('.settings-team-tag')].map((t) => t.textContent)).toEqual([
      'gui.settings.team.host_model', 'gui.settings.team.host_keys',
    ])
  })

  it('hires in one step on this Raven keys, with its own model and prompt', async () => {
    wire([])
    const hire = vi.spyOn(teamSource, 'hire').mockResolvedValue({ employee: { ...mira, name: 'Anna', folder: 'anna' } })
    render(<Team />)
    await settle()
    const button = document.querySelector('.settings-team-hire-btn') as HTMLButtonElement
    expect(button.disabled).toBe(true)

    fireEvent.change(field('gui.settings.team.name'), { target: { value: 'Anna' } })
    fireEvent.change(field('gui.settings.team.role'), { target: { value: 'Writes copy.' } })
    fireEvent.change(field('gui.settings.team.model'), { target: { value: 'gpt-4o-mini' } })
    fireEvent.change(field('gui.settings.team.prompt'), { target: { value: 'You are Anna.' } })
    expect(button.disabled).toBe(false)
    fireEvent.click(button)
    await settle()

    expect(hire).toHaveBeenCalledWith({
      name: 'Anna', role: 'Writes copy.', owns: '', system_prompt: 'You are Anna.', provider: '', model: 'gpt-4o-mini', api_key: '', api_base: '',
    })
    expect(document.querySelector('.settings-team-result')?.textContent).toBe('gui.settings.team.hired {"name":"Anna"}')
  })

  it('asks for the provider, model and key when the employee has a key of its own', async () => {
    wire([])
    const hire = vi.spyOn(teamSource, 'hire').mockResolvedValue({ employee: { ...mira, name: 'Bo', own_key: true } })
    render(<Team />)
    await settle()
    fireEvent.click(screen.getByText('gui.settings.team.access_own'))
    fireEvent.change(field('gui.settings.team.name'), { target: { value: 'Bo' } })
    fireEvent.change(field('gui.settings.team.role'), { target: { value: 'Codes.' } })
    fireEvent.change(field('gui.settings.team.prompt'), { target: { value: 'You are Bo.' } })
    fireEvent.change(field('gui.settings.team.model'), { target: { value: 'claude-sonnet-4-5' } })
    const button = document.querySelector('.settings-team-hire-btn') as HTMLButtonElement
    expect(button.disabled).toBe(true)

    fireEvent.change(field('gui.settings.team.provider'), { target: { value: 'anthropic' } })
    fireEvent.change(field('gui.settings.team.api_key'), { target: { value: 'sk-x' } })
    fireEvent.click(button)
    await settle()

    expect(hire.mock.calls[0]?.[0]).toMatchObject({ provider: 'anthropic', model: 'claude-sonnet-4-5', api_key: 'sk-x' })
    expect((field('gui.settings.team.api_key') as HTMLInputElement).value).toBe('')
  })

  it('edits the prompt and retires after a second click', async () => {
    wire()
    vi.spyOn(teamSource, 'prompt').mockResolvedValue('You are Mira.')
    const setPrompt = vi.spyOn(teamSource, 'setPrompt').mockResolvedValue(true)
    const retire = vi.spyOn(teamSource, 'retire').mockResolvedValue('employees/.retired/mira-1')
    render(<Team />)
    await settle()

    fireEvent.click(screen.getByText('gui.settings.team.edit_prompt'))
    await settle()
    const box = document.querySelector('[data-employee="mira"] textarea') as HTMLTextAreaElement
    expect(box.value).toBe('You are Mira.')
    fireEvent.change(box, { target: { value: 'You are Mira. Be brief.' } })
    fireEvent.click(screen.getByText('gui.settings.team.save_prompt'))
    await settle()
    expect(setPrompt).toHaveBeenCalledWith('Mira', 'You are Mira. Be brief.')

    fireEvent.click(screen.getByText('gui.settings.team.retire'))
    expect(retire).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('gui.settings.team.retire_confirm {"name":"Mira"}'))
    await settle()
    expect(retire).toHaveBeenCalledWith('Mira')
  })
})
