/* The team section: the employees hired, and hiring one in one step.
 *
 * An employee is an agent of the reader's own -- a name, a role the main agent
 * reads to decide when to hand it work, a model on this Raven's keys or on a key
 * of its own, and a system prompt -- that starts with no conversations
 * (raven/agent/subagent/hire.py, the same step as `raven agents hire`). The
 * roster picks a hire up at once. A key typed here goes into the employee's own
 * .env and is never shown again.
 */

import { useEffect, useState } from 'react'

import { t } from '../../../i18n/t'
import { teamSource } from '../source'

import type { ResultOf } from '../../../rpc/generated'
import type { JSX } from 'react'

type Row = ResultOf<'employees.list'>['employees'][number]

const said = (e: unknown): string =>
  String((e as { message?: string } | null)?.message || (e as { data?: { detail?: string } } | null)?.data?.detail || e)

function Card({ row, onRetired }: { row: Row; onRetired: (note: string) => void }): JSX.Element {
  const [prompt, setPrompt] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [confirming, setConfirming] = useState(false)
  const openPrompt = (): void => {
    if (prompt !== null) { setPrompt(null); return }
    teamSource.prompt(row.name).then(setPrompt, (e) => setNote(said(e)))
  }
  const savePrompt = (): void => {
    if (prompt === null) return
    teamSource.setPrompt(row.name, prompt).then(() => setNote(t('gui.settings.team.prompt_saved')), (e) => setNote(said(e)))
  }
  const retire = (): void => {
    if (!confirming) { setConfirming(true); return }
    teamSource.retire(row.name).then((kept) => onRetired(t('gui.settings.team.retired', { name: row.name, path: kept })),
      (e) => { setConfirming(false); setNote(said(e)) })
  }
  return (
    <section className="settings-team-card" data-employee={row.folder}>
      <div className="settings-team-row">
        <strong>{row.name}</strong>
        <span className="settings-team-tag">{row.model || t('gui.settings.team.host_model')}</span>
        <span className="settings-team-tag">{row.own_key ? t('gui.settings.team.own_key') : t('gui.settings.team.host_keys')}</span>
      </div>
      <p className="settings-team-role">{row.role}</p>
      <div className="settings-team-actions">
        <button type="button" onClick={openPrompt}>{t('gui.settings.team.edit_prompt')}</button>
        <button type="button" className="settings-team-retire" onClick={retire}>
          {confirming ? t('gui.settings.team.retire_confirm', { name: row.name }) : t('gui.settings.team.retire')}
        </button>
      </div>
      {prompt !== null ? (
        <div className="settings-team-form">
          <textarea rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <div className="settings-team-actions">
            <button type="button" onClick={savePrompt}>{t('gui.settings.team.save_prompt')}</button>
          </div>
        </div>
      ) : null}
      {note ? <p className="settings-team-note">{note}</p> : null}
    </section>
  )
}

export function Team(): JSX.Element {
  const [rows, setRows] = useState<Row[] | null>(null)
  const [configured, setConfigured] = useState<string[]>([])
  const [all, setAll] = useState<string[]>([])
  const [name, setName] = useState('')
  const [role, setRole] = useState('')
  const [owns, setOwns] = useState('')
  const [own, setOwn] = useState(false)
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [api_key, setApiKey] = useState('')
  const [api_base, setApiBase] = useState('')
  const [prompt, setPrompt] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = (): void => {
    teamSource.list().then((r) => {
      setRows(r.employees)
      setConfigured(r.providers)
      setAll(r.all_providers)
    }, (e) => { setRows([]); setNote(said(e)) })
  }
  useEffect(refresh, [])

  const ready = name.trim() && role.trim() && prompt.trim() && (!own || (provider && model.trim() && api_key.trim()))
  const hire = (): void => {
    setBusy(true)
    setNote('')
    teamSource.hire({
      name: name.trim(), role: role.trim(), owns: owns.trim(), system_prompt: prompt,
      provider, model: model.trim(), api_key: own ? api_key.trim() : '', api_base: own ? api_base.trim() : '',
    }).then((r) => {
      setNote(t('gui.settings.team.hired', { name: r.employee.name }))
      setName(''); setRole(''); setOwns(''); setModel(''); setApiKey(''); setApiBase(''); setPrompt('')
      refresh()
    }, (e) => setNote(said(e))).finally(() => setBusy(false))
  }
  const providers = own ? all : configured

  return (
    <div className="settings-team">
      <p className="settings-team-intro">{t('gui.settings.team.intro')}</p>
      <div className="settings-team-list">
        {rows === null ? <span>{t('gui.settings.team.loading')}</span> : null}
        {rows?.length === 0 ? <span className="settings-team-empty">{t('gui.settings.team.empty')}</span> : null}
        {(rows || []).map((row) => (
          <Card key={row.folder} row={row} onRetired={(text) => { setNote(text); refresh() }} />
        ))}
      </div>
      <section className="settings-team-form settings-team-hire">
        <h3>{t('gui.settings.team.hire_title')}</h3>
        <label className="settings-team-field">
          <span>{t('gui.settings.team.name')}</span>
          <input value={name} maxLength={48} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="settings-team-field">
          <span>{t('gui.settings.team.role')}</span>
          <textarea rows={2} value={role} onChange={(e) => setRole(e.target.value)} />
          <small>{t('gui.settings.team.role_hint')}</small>
        </label>
        <label className="settings-team-field">
          <span>{t('gui.settings.team.owns')}</span>
          <input value={owns} onChange={(e) => setOwns(e.target.value)} />
        </label>
        <div className="settings-team-row" role="radiogroup" aria-label={t('gui.settings.team.access')}>
          <label className="settings-team-check">
            <input type="radio" name="settings-team-access" checked={!own} onChange={() => { setOwn(false); setProvider('') }} />
            <span>{t('gui.settings.team.access_host')}</span>
          </label>
          <label className="settings-team-check">
            <input type="radio" name="settings-team-access" checked={own} onChange={() => { setOwn(true); setProvider('') }} />
            <span>{t('gui.settings.team.access_own')}</span>
          </label>
        </div>
        <div className="settings-team-row">
          <label className="settings-team-field">
            <span>{t('gui.settings.team.provider')}</span>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              {own ? <option value="" disabled>-</option> : <option value="">{t('gui.settings.team.provider_host')}</option>}
              {providers.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
          <label className="settings-team-field">
            <span>{t('gui.settings.team.model')}</span>
            <input value={model} onChange={(e) => setModel(e.target.value)} />
            {own ? null : <small>{t('gui.settings.team.model_hint')}</small>}
          </label>
        </div>
        {own ? (
          <div className="settings-team-row">
            <label className="settings-team-field">
              <span>{t('gui.settings.team.api_key')}</span>
              <input type="password" autoComplete="off" value={api_key} onChange={(e) => setApiKey(e.target.value)} />
            </label>
            <label className="settings-team-field">
              <span>{t('gui.settings.team.api_base')}</span>
              <input value={api_base} onChange={(e) => setApiBase(e.target.value)} />
            </label>
          </div>
        ) : null}
        <label className="settings-team-field">
          <span>{t('gui.settings.team.prompt')}</span>
          <textarea rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <small>{t('gui.settings.team.prompt_hint')}</small>
        </label>
        <div className="settings-team-actions">
          <button type="button" className="settings-team-hire-btn" disabled={!ready || busy} onClick={hire}>
            {t('gui.settings.team.hire')}
          </button>
        </div>
      </section>
      {note ? <p className="settings-team-note settings-team-result">{note}</p> : null}
    </div>
  )
}
