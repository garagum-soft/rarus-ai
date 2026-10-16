/* The workflows section: a playbook's graph of steps, edited as cards and shown
 * as the waves it will run in.
 *
 * The cards are the steps as the file writes them -- one step, a step for each
 * item, a step repeated over several passes, each with an optional step that
 * collects the results -- and the column beside them is what those expand to,
 * as the server expands and checks it (playbooks.graph.check) on every pause
 * in typing: wave by wave, every step of a wave running at once. Saving writes
 * the steps as written into the user's own library; a builtin is saved as the
 * user's own copy, never edited. Nothing here runs a workflow: it is the same
 * playbook the agent loads and the CLI runs.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { t } from '../../../i18n/t'
import { workflowSource } from '../source'
import {
  blankStep, fromRaw, removeStep, renameStep, toRaw, toggleDep, updateStep, waves,
} from '../workflowModel'

import type { ResultOf } from '../../../rpc/generated'
import type { Draft, StepKind } from '../workflowModel'
import type { JSX } from 'react'

type Raw = Record<string, unknown>
type Row = ResultOf<'playbooks.list'>['playbooks'][number]

const CHECK_AFTER_MS = 400

const said = (e: unknown): string =>
  String((e as { message?: string } | null)?.message || (e as { data?: { detail?: string } } | null)?.data?.detail || e)

function StepCard({ step, others, onChange, onRename, onRemove, onToggleDep }: {
  step: Draft
  others: string[]
  onChange: (patch: Partial<Draft>) => void
  onRename: (to: string) => void
  onRemove: () => void
  onToggleDep: (dep: string) => void
}): JSX.Element {
  const [id, setId] = useState(step.id)
  useEffect(() => setId(step.id), [step.id])
  return (
    <section className="settings-wf-step" data-step={step.id} data-kind={step.kind}>
      <div className="settings-wf-row">
        <label className="settings-wf-field">
          <span>{t('gui.settings.wf.step_id')}</span>
          <input value={id} onChange={(e) => setId(e.target.value)}
            onBlur={() => { if (id.trim() && id !== step.id) onRename(id.trim()) }} />
        </label>
        <label className="settings-wf-field">
          <span>{t('gui.settings.wf.agent')}</span>
          <input list="settings-wf-agents" value={step.subagent} onChange={(e) => onChange({ subagent: e.target.value })} />
        </label>
        <label className="settings-wf-field">
          <span>{t('gui.settings.wf.type')}</span>
          <select value={step.kind} disabled={step.kind === 'branches'}
            onChange={(e) => onChange({ kind: e.target.value as StepKind })}>
            <option value="step">{t('gui.settings.wf.type_step')}</option>
            <option value="forEach">{t('gui.settings.wf.type_for_each')}</option>
            <option value="repeat">{t('gui.settings.wf.type_repeat')}</option>
            {step.kind === 'branches' ? <option value="branches">{t('gui.settings.wf.type_branches')}</option> : null}
          </select>
        </label>
        <button type="button" className="settings-wf-remove" onClick={onRemove}>{t('gui.settings.wf.remove_step')}</button>
      </div>
      <label className="settings-wf-field">
        <span>{t('gui.settings.wf.summary')}</span>
        <input value={step.summary} onChange={(e) => onChange({ summary: e.target.value })} />
      </label>
      <label className="settings-wf-field">
        <span>{t('gui.settings.wf.prompt')}</span>
        <textarea rows={3} value={step.prompt} onChange={(e) => onChange({ prompt: e.target.value })} />
      </label>
      {step.kind === 'forEach' ? (
        <label className="settings-wf-field">
          <span>{t('gui.settings.wf.items')}</span>
          <textarea rows={3} value={step.items} onChange={(e) => onChange({ items: e.target.value })} />
          <small>{t('gui.settings.wf.items_hint')}</small>
        </label>
      ) : null}
      {step.kind === 'repeat' ? (
        <label className="settings-wf-field settings-wf-passes">
          <span>{t('gui.settings.wf.passes')}</span>
          <input type="number" min={2} max={10} value={step.passes}
            onChange={(e) => onChange({ passes: Math.max(2, Math.min(10, Number(e.target.value) || 2)) })} />
          <small>{t('gui.settings.wf.passes_hint')}</small>
        </label>
      ) : null}
      {step.kind !== 'step' ? (
        <div className="settings-wf-collect">
          <label className="settings-wf-check">
            <input type="checkbox" checked={step.collect} onChange={(e) => onChange({ collect: e.target.checked })} />
            <span>{t('gui.settings.wf.collect')}</span>
          </label>
          {step.collect ? (
            <label className="settings-wf-field">
              <textarea rows={2} value={step.collectPrompt} placeholder="{{ results }}"
                onChange={(e) => onChange({ collectPrompt: e.target.value })} />
              <small>{t('gui.settings.wf.collect_hint')}</small>
            </label>
          ) : null}
        </div>
      ) : null}
      {others.length ? (
        <div className="settings-wf-deps">
          <span>{t('gui.settings.wf.waits_on')}</span>
          {others.map((o) => (
            <label key={o} className="settings-wf-check">
              <input type="checkbox" checked={step.dependsOn.includes(o)} onChange={() => onToggleDep(o)} />
              <span>{o}</span>
            </label>
          ))}
        </div>
      ) : null}
    </section>
  )
}

export function Workflows(): JSX.Element {
  const [library, setLibrary] = useState<Row[] | null>(null)
  const [agents, setAgents] = useState<string[]>([])
  const [name, setName] = useState('')
  const [opened, setOpened] = useState<string | null>(null)
  const [origin, setOrigin] = useState<string | null>(null)
  const [description, setDescription] = useState('')
  const [steps, setSteps] = useState<Draft[] | null>(null)
  const [expanded, setExpanded] = useState<Raw[]>([])
  const [errors, setErrors] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const checkSeq = useRef(0)

  const refresh = (): void => {
    workflowSource.list().then(setLibrary, (e) => { setLibrary([]); setNote(said(e)) })
  }
  useEffect(() => {
    refresh()
    workflowSource.agents().then(setAgents, () => setAgents([]))
  }, [])

  const raw = useMemo(() => (steps || []).map(toRaw), [steps])
  useEffect(() => {
    if (!steps) return undefined
    const seq = ++checkSeq.current
    const timer = setTimeout(() => {
      workflowSource.check(raw).then((r) => {
        if (seq !== checkSeq.current) return
        setExpanded(r.expanded || [])
        setErrors(r.errors || [])
      }, (e) => { if (seq === checkSeq.current) setErrors([said(e)]) })
    }, CHECK_AFTER_MS)
    return () => clearTimeout(timer)
  }, [raw, steps])

  const open = (which: string): void => {
    setNote('')
    workflowSource.get(which).then((r) => {
      setOpened(r.name)
      setName(r.name)
      setOrigin(r.origin)
      setDescription(r.description)
      setSteps(r.nodes.map((n) => fromRaw(n as Raw)))
      setExpanded(r.expanded || [])
      setErrors(r.errors || [])
    }, (e) => setNote(said(e)))
  }
  const start = (): void => {
    setOpened(null)
    setOrigin(null)
    setName('')
    setDescription('')
    setNote('')
    setSteps([blankStep([], agents[0] || 'Raven')])
  }
  const save = (): void => {
    if (!steps) return
    setBusy(true)
    setNote('')
    const overwrite = opened !== null && name === opened
    workflowSource.save(name.trim(), raw, description, overwrite).then((r) => {
      setOpened(r.name)
      setOrigin(r.origin)
      setExpanded(r.expanded || [])
      setErrors(r.errors || [])
      setNote(t(r.shadows_builtin ? 'gui.settings.wf.saved_shadow' : 'gui.settings.wf.saved', { path: r.path }))
      refresh()
    }, (e) => setNote(said(e))).finally(() => setBusy(false))
  }

  const ids = (steps || []).map((s) => s.id)
  const plan = useMemo(() => waves(expanded), [expanded])

  return (
    <div className="settings-wf">
      <p className="settings-wf-intro">{t('gui.settings.wf.intro')}</p>
      <div className="settings-wf-library">
        {library === null ? <span>{t('gui.settings.wf.loading')}</span> : null}
        {library?.length === 0 ? <span>{t('gui.settings.wf.empty')}</span> : null}
        {(library || []).map((p) => (
          <button key={p.name} type="button" className="settings-wf-pb" aria-pressed={opened === p.name}
            data-origin={p.origin} onClick={() => open(p.name)}>
            {p.name}
          </button>
        ))}
        <button type="button" className="settings-wf-new" onClick={start}>{t('gui.settings.wf.new')}</button>
      </div>
      {steps === null ? <p className="settings-wf-pick">{t('gui.settings.wf.pick')}</p> : (
        <div className="settings-wf-body">
          <div className="settings-wf-form">
            <label className="settings-wf-field">
              <span>{t('gui.settings.wf.name')}</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-workflow" />
            </label>
            <label className="settings-wf-field">
              <span>{t('gui.settings.wf.description')}</span>
              <input value={description} onChange={(e) => setDescription(e.target.value)} />
              <small>{t('gui.settings.wf.description_hint')}</small>
            </label>
            {origin === 'builtin' ? <p className="settings-wf-builtin">{t('gui.settings.wf.builtin')}</p> : null}
            <datalist id="settings-wf-agents">{agents.map((a) => <option key={a} value={a} />)}</datalist>
            {steps.map((step) => (
              <StepCard key={step.id} step={step}
                others={ids.filter((i) => i !== step.id)}
                onChange={(patch) => setSteps((s) => updateStep(s || [], step.id, patch))}
                onRename={(to) => setSteps((s) => renameStep(s || [], step.id, to))}
                onRemove={() => setSteps((s) => removeStep(s || [], step.id))}
                onToggleDep={(dep) => setSteps((s) => toggleDep(s || [], step.id, dep))} />
            ))}
            <button type="button" className="settings-wf-add"
              onClick={() => setSteps((s) => [...(s || []), blankStep(s || [], agents[0] || 'Raven')])}>
              {t('gui.settings.wf.add_step')}
            </button>
          </div>
          <div className="settings-wf-side">
            <h4>{t('gui.settings.wf.preview')}</h4>
            <ol className="settings-wf-waves">
              {plan.map((wave, i) => (
                <li key={i} className="settings-wf-wave" data-wave={i + 1}>
                  {wave.map((s) => (
                    <div key={s.id} className="settings-wf-node" data-node={s.id}>
                      <b>{s.summary}</b>
                      <span>{s.agent}</span>
                      {s.after.length ? <small>{t('gui.settings.wf.after', { ids: s.after.join(', ') })}</small> : null}
                    </div>
                  ))}
                </li>
              ))}
            </ol>
            {errors.length ? (
              <div className="settings-wf-errors">
                <h4>{t('gui.settings.wf.errors')}</h4>
                <ul>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </div>
            ) : <p className="settings-wf-ok">{t('gui.settings.wf.no_errors')}</p>}
            <button type="button" className="settings-wf-save" disabled={busy || !name.trim() || errors.length > 0} onClick={save}>
              {t('gui.settings.wf.save')}
            </button>
            {note ? <p className="settings-wf-note" role="status">{note}</p> : null}
          </div>
        </div>
      )}
    </div>
  )
}
