/* Instructions and memory: the agent's own files (who it is, how it works,
   the notes on its tools) and what it remembers about the reader, edited in
   place. Every save is checked against the text the editor opened -- a file
   the agent or the memory engine rewrote meanwhile is not overwritten unseen
   -- and kept as a revision the history below can show and put back. */
import { useEffect } from 'react'

import { t } from '../../../i18n/t'
import * as confirm from '../../../state/confirm'
import { whenLabel } from '../../rail/source'
import { Card, Crumb, Empty, GLYPH, Row, Rov, Spin } from '../Fields'
import * as store from '../store'

import type { InstrFile } from '../types'
import type { JSX } from 'react'

/* Literal keys, so the i18n gate reads each one. */
const LABEL: Record<string, [string, string]> = {
  'agent_memory/profile/soul.md': ['gui.settings.instr.soul', 'gui.settings.instr.soul_sub'],
  'agent_memory/profile/agent.md': ['gui.settings.instr.agent', 'gui.settings.instr.agent_sub'],
  'TOOLS.md': ['gui.settings.instr.tools', 'gui.settings.instr.tools_sub'],
  'user_memory/profile/user.md': ['gui.settings.instr.user', 'gui.settings.instr.user_sub'],
  'user_memory/attention.md': ['gui.settings.instr.attention', 'gui.settings.instr.attention_sub'],
  'user_memory/behaviors.md': ['gui.settings.instr.behaviors', 'gui.settings.instr.behaviors_sub'],
  'HEARTBEAT.md': ['gui.settings.instr.heartbeat', 'gui.settings.instr.heartbeat_sub'],
}

const label = (key: string): string => (LABEL[key] ? t(LABEL[key][0]) : key)
const blurb = (key: string): string => (LABEL[key] ? t(LABEL[key][1]) : '')

function kb(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}

function FileRow({ f }: { f: InstrFile }): JSX.Element {
  const meta = f.exists
    ? t('gui.settings.instr.meta', { size: kb(f.size), n: f.revisions })
    : t('gui.settings.instr.missing')
  return (
    <Row label={<>{label(f.key)} <span className="settings-kk">{f.key}</span></>} sub={`${blurb(f.key)} ${meta}`}>
      <button type="button" className="mini" onClick={() => { void store.instrPick(f.key) }}>
        {t('gui.settings.instr.open')}
      </button>
    </Row>
  )
}

function FileList(): JSX.Element {
  const files = store.get().instrFiles
  if (files === null) return <Card><Spin>{t('gui.settings.instr.loading')}</Spin></Card>
  const of = (kind: string): InstrFile[] => files.filter((f) => f.kind === kind)
  return (
    <>
      <Card title={t('gui.settings.instr.instructions')}>
        {of('instruction').map((f) => <FileRow key={f.key} f={f} />)}
      </Card>
      <Card title={t('gui.settings.instr.memory')}>
        {of('memory').length === 0
          ? <Empty icon={GLYPH.pick}>{t('gui.settings.instr.none')}</Empty>
          : of('memory').map((f) => <FileRow key={f.key} f={f} />)}
      </Card>
    </>
  )
}

function Editor(): JSX.Element {
  const s = store.get()
  const open = s.instrOpen
  const key = s.instrKey ?? ''
  if (!open) {
    return (
      <>
        <Crumb back={t('gui.settings.instr.back')} onBack={() => { void store.instrPick(null) }} name={label(key)} />
        <Card><Spin>{t('gui.settings.instr.loading')}</Spin></Card>
      </>
    )
  }
  const dirty = s.instrDraft !== null
  const save = (): void => { void store.instrSave() }
  const revert = (): void => {
    confirm.ask(
      t('gui.settings.instr.revert_title'), t('gui.settings.instr.revert_body'), t('gui.settings.instr.revert'),
      () => { void store.instrPick(open.key) },
    )
  }
  const restore = (rev: number): void => {
    confirm.ask(
      t('gui.settings.instr.restore_title', { rev }), t('gui.settings.instr.restore_body'), t('gui.settings.instr.restore'),
      () => { void store.instrRollback(rev) },
    )
  }
  return (
    <>
      <Crumb back={t('gui.settings.instr.back')} onBack={() => { void store.instrPick(null) }} name={label(open.key)}>
        <span className="settings-kk">{open.key}</span>
      </Crumb>
      {s.instrConflict && (
        <Card>
          <Row label={<Rov warn>{t('gui.settings.instr.conflict')}</Rov>} sub={t('gui.settings.instr.conflict_sub')} stack>
            <span className="settings-taglist">
              <button type="button" className="mini" onClick={() => { void store.instrPick(open.key, true) }}>
                {t('gui.settings.instr.keep_mine')}
              </button>
              <button type="button" className="mini ghost" onClick={() => { void store.instrPick(open.key) }}>
                {t('gui.settings.instr.take_theirs')}
              </button>
            </span>
          </Row>
        </Card>
      )}
      <Card
        title={blurb(open.key)}
        act={(
          <span className="settings-taglist">
            {dirty && <Rov>{t('gui.settings.instr.unsaved')}</Rov>}
            <button type="button" className="mini ghost" disabled={!dirty} onClick={revert}>{t('gui.settings.instr.revert')}</button>
            <button type="button" className="mini go" disabled={!dirty || store.isBusy('instr:save')} onClick={save}>
              {t('gui.settings.instr.save')}
            </button>
          </span>
        )}
        raw
      >
        {/* Uncontrolled, keyed by the file and the text it holds: a new base
            remounts it, a page remount seeds it from the draft. */}
        <textarea
          key={`${open.key}:${open.sha256}`}
          className="settings-instr-text"
          spellCheck={false}
          aria-label={label(open.key)}
          defaultValue={s.instrDraft ?? open.text}
          onInput={(e) => store.instrEdit((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
              e.preventDefault()
              save()
            }
          }}
        />
      </Card>
      <Card title={t('gui.settings.instr.history')}>
        {open.revisions.length === 0 && <Empty icon={GLYPH.archive}>{t('gui.settings.instr.no_history')}</Empty>}
        {open.revisions.map((r) => (
          <Row
            key={r.rev}
            label={<>#{r.rev} <span className="settings-kk">{r.author} · {whenLabel(r.at)}</span></>}
            sub={r.action === 'rollback' ? t('gui.settings.instr.rolled_back', { rev: r.restored_from ?? 0 }) : r.note}
            open={s.instrPeek?.rev === r.rev}
          >
            <span className="settings-taglist">
              <button type="button" className="mini ghost" onClick={() => { void store.instrPeek(s.instrPeek?.rev === r.rev ? null : r.rev) }}>
                {s.instrPeek?.rev === r.rev ? t('gui.settings.instr.hide') : t('gui.settings.instr.view')}
              </button>
              {r.rev !== open.head && (
                <button type="button" className="mini" disabled={store.isBusy('instr:rollback')} onClick={() => restore(r.rev)}>
                  {t('gui.settings.instr.restore')}
                </button>
              )}
            </span>
          </Row>
        ))}
        {s.instrPeek && <pre className="settings-instr-peek">{s.instrPeek.text}</pre>}
      </Card>
    </>
  )
}

export function Instructions(): JSX.Element {
  const s = store.get()
  useEffect(() => {
    if (s.instrFiles === null) void store.instrLoad()
  }, [s.instrFiles])
  return s.instrKey === null ? <FileList /> : <Editor />
}
