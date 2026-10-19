/* The canvas's right-click menu and its small dialogs: a new company (name and logo), a name
 * (department, rename), a new employee (name, role, prompt, avatar), a picture picker
 * (avatars, backgrounds) and a head picker. Each dialog is a scrim over the canvas with one
 * form; Escape or a click on the scrim cancels. */

import { useEffect, useRef, useState } from 'react'

import { t } from '../../i18n/t'
import { AVATARS, BACKDROPS, letterLogo } from './art'

import type { JSX, ReactNode } from 'react'

export interface MenuItem {
  label: string
  run: () => void
  danger?: boolean
  disabled?: boolean
}

/** A menu at a point of the stage (stage pixels); closes on a pick, Escape, or a press elsewhere. */
export function ContextMenu({ x, y, title, items, onClose }: {
  x: number; y: number; title?: string; items: MenuItem[]; onClose: () => void
}): JSX.Element {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const away = (e: PointerEvent): void => { if (!box.current?.contains(e.target as Node)) onClose() }
    const esc = (e: KeyboardEvent): void => { if (e.key === 'Escape') onClose() }
    document.addEventListener('pointerdown', away, true)
    document.addEventListener('keydown', esc)
    box.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
    return () => {
      document.removeEventListener('pointerdown', away, true)
      document.removeEventListener('keydown', esc)
    }
  }, [onClose])
  return (
    <div className="canvas-menu" role="menu" ref={box} style={{ left: x, top: y }} onContextMenu={(e) => e.preventDefault()}>
      {title ? <div className="canvas-menu-title">{title}</div> : null}
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          className="canvas-menu-item"
          data-danger={item.danger || undefined}
          disabled={item.disabled}
          onClick={() => { onClose(); item.run() }}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

function Dialog({ title, children, onClose, wide }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }): JSX.Element {
  useEffect(() => {
    const esc = (e: KeyboardEvent): void => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [onClose])
  return (
    <div className="canvas-scrim" onPointerDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="canvas-dialog" data-wide={wide || undefined} role="dialog" aria-modal="true" aria-label={title}>
        <div className="canvas-dialog-title">{title}</div>
        {children}
      </div>
    </div>
  )
}

function Buttons({ ok, onCancel, disabled }: { ok: string; onCancel: () => void; disabled?: boolean }): JSX.Element {
  return (
    <div className="canvas-dialog-row">
      <button type="button" className="canvas-tool" onClick={onCancel}>{t('gui.canvas.d_cancel')}</button>
      <button type="submit" className="canvas-save" disabled={disabled}>{ok}</button>
    </div>
  )
}

/* A picture file shrunk to fit 128 px and turned into a PNG data URL, small enough for the config. */
async function shrink(file: File): Promise<string> {
  const url = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
  const img = await new Promise<HTMLImageElement>((resolve, reject) => {
    const el = new Image()
    el.onload = () => resolve(el)
    el.onerror = () => reject(new Error('not a picture'))
    el.src = url
  })
  const side = 128
  const canvas = document.createElement('canvas')
  canvas.width = side
  canvas.height = side
  const ctx = canvas.getContext('2d')
  if (!ctx) return url
  const k = Math.min(side / img.width, side / img.height)
  const w = img.width * k
  const h = img.height * k
  ctx.drawImage(img, (side - w) / 2, (side - h) / 2, w, h)
  return canvas.toDataURL('image/png')
}

/** A new company, or an existing one's name and logo. */
export function CompanyDialog({ title, name: start, logo: startLogo, ok, onSubmit, onClose }: {
  title: string; name?: string; logo?: string; ok: string; onSubmit: (name: string, logo: string) => void; onClose: () => void
}): JSX.Element {
  const [name, setName] = useState(start ?? '')
  const [logo, setLogo] = useState(startLogo ?? '')
  const [bad, setBad] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const pick = async (f: File | undefined): Promise<void> => {
    if (!f) return
    try {
      setLogo(await shrink(f))
      setBad(false)
    } catch {
      setBad(true)
    }
  }
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="canvas-dialog-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) onSubmit(name.trim(), logo) }}>
        <label className="canvas-field">
          <span>{t('gui.canvas.d_name')}</span>
          <input autoFocus value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="canvas-field">
          <span>{t('gui.canvas.d_logo')}</span>
          <div className="canvas-logo-row">
            <img className="canvas-logo-big" src={logo || letterLogo(name || '?')} alt="" />
            <button type="button" className="canvas-tool" onClick={() => file.current?.click()}>{t('gui.canvas.d_logo_pick')}</button>
            {logo ? <button type="button" className="canvas-tool" onClick={() => setLogo('')}>{t('gui.canvas.d_logo_clear')}</button> : null}
            <input ref={file} type="file" accept="image/*" hidden onChange={(e) => void pick(e.target.files?.[0])} />
          </div>
          <span className="canvas-faint">{bad ? t('gui.canvas.d_logo_bad') : t('gui.canvas.d_logo_hint')}</span>
        </div>
        <Buttons ok={ok} onCancel={onClose} disabled={!name.trim()} />
      </form>
    </Dialog>
  )
}

/** One name: a new department, or renaming a company or a department. */
export function NameDialog({ title, value, ok, onSubmit, onClose }: {
  title: string; value?: string; ok: string; onSubmit: (name: string) => void; onClose: () => void
}): JSX.Element {
  const [name, setName] = useState(value ?? '')
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="canvas-dialog-form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) onSubmit(name.trim()) }}>
        <label className="canvas-field">
          <span>{t('gui.canvas.d_name')}</span>
          <input autoFocus value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        </label>
        <Buttons ok={ok} onCancel={onClose} disabled={!name.trim()} />
      </form>
    </Dialog>
  )
}

export interface HireForm {
  name: string
  role: string
  prompt: string
  avatar: string
}

/** A new employee: name, what it does, its system prompt and its picture. */
export function HireDialog({ where, busy, onSubmit, onClose }: {
  where: string; busy: boolean; onSubmit: (form: HireForm) => void; onClose: () => void
}): JSX.Element {
  const [name, setName] = useState('')
  const [role, setRole] = useState('')
  const [prompt, setPrompt] = useState('')
  const [touched, setTouched] = useState(false)
  const [avatar, setAvatar] = useState(AVATARS[0]?.id ?? '')
  const seed = t('gui.canvas.d_hire_prompt_seed', { name: name || '…', role: role || '…' })
  const text = touched ? prompt : seed
  const ready = /[A-Za-z0-9]/.test(name) && !!role.trim() && !!text.trim() && !busy
  return (
    <Dialog title={t('gui.canvas.d_hire')} onClose={onClose} wide>
      <form className="canvas-dialog-form" onSubmit={(e) => { e.preventDefault(); if (ready) onSubmit({ name: name.trim(), role: role.trim(), prompt: text.trim(), avatar }) }}>
        <p className="canvas-text canvas-faint">{where}</p>
        <label className="canvas-field">
          <span>{t('gui.canvas.d_hire_name')}</span>
          <input autoFocus value={name} maxLength={48} onChange={(e) => setName(e.target.value)} />
          <span className="canvas-faint">{t('gui.canvas.d_hire_name_hint')}</span>
        </label>
        <label className="canvas-field">
          <span>{t('gui.canvas.d_hire_role')}</span>
          <input value={role} maxLength={300} onChange={(e) => setRole(e.target.value)} />
          <span className="canvas-faint">{t('gui.canvas.d_hire_role_hint')}</span>
        </label>
        <label className="canvas-field">
          <span>{t('gui.canvas.d_hire_prompt')}</span>
          <textarea className="canvas-prompt canvas-prompt-short" value={text} onChange={(e) => { setTouched(true); setPrompt(e.target.value) }} />
        </label>
        <div className="canvas-field">
          <span>{t('gui.canvas.d_hire_avatar')}</span>
          <AvatarGrid value={avatar} onPick={setAvatar} />
        </div>
        <Buttons ok={busy ? t('gui.canvas.d_hiring') : t('gui.canvas.d_hire_ok')} onCancel={onClose} disabled={!ready} />
      </form>
    </Dialog>
  )
}

function AvatarGrid({ value, onPick }: { value: string; onPick: (id: string) => void }): JSX.Element {
  return (
    <div className="canvas-avatars" role="radiogroup" aria-label={t('gui.canvas.d_hire_avatar')}>
      {AVATARS.map((a) => (
        <button key={a.id} type="button" role="radio" aria-checked={value === a.id} className="canvas-avatar-opt" onClick={() => onPick(a.id)}>
          <img src={a.src} alt="" />
        </button>
      ))}
    </div>
  )
}

/** Picks one of the cartoon avatars. */
export function AvatarDialog({ value, onSubmit, onClose }: { value: string; onSubmit: (id: string) => void; onClose: () => void }): JSX.Element {
  const [pick, setPick] = useState(value || AVATARS[0]?.id || '')
  return (
    <Dialog title={t('gui.canvas.d_avatar')} onClose={onClose} wide>
      <form className="canvas-dialog-form" onSubmit={(e) => { e.preventDefault(); onSubmit(pick) }}>
        <AvatarGrid value={pick} onPick={setPick} />
        <Buttons ok={t('gui.canvas.d_save')} onCancel={onClose} />
      </form>
    </Dialog>
  )
}

/** Picks one of the office backgrounds. */
export function BackdropDialog({ value, onSubmit, onClose }: { value: string; onSubmit: (id: string) => void; onClose: () => void }): JSX.Element {
  const [pick, setPick] = useState(value)
  return (
    <Dialog title={t('gui.canvas.d_bg')} onClose={onClose} wide>
      <form className="canvas-dialog-form" onSubmit={(e) => { e.preventDefault(); onSubmit(pick) }}>
        <div className="canvas-bgs" role="radiogroup" aria-label={t('gui.canvas.d_bg')}>
          {BACKDROPS.map((b) => (
            <button key={b.id} type="button" role="radio" aria-checked={pick === b.id} className="canvas-bg-opt" onClick={() => setPick(b.id)}>
              <span className="canvas-bg-swatch" style={{ background: b.css }} />
              <span>{t(b.key)}</span>
            </button>
          ))}
        </div>
        <Buttons ok={t('gui.canvas.d_save')} onCancel={onClose} />
      </form>
    </Dialog>
  )
}

/** Picks who heads a department or a company, from the people who work there. */
export function HeadDialog({ title, people, value, onSubmit, onClose }: {
  title: string; people: string[]; value: string; onSubmit: (name: string) => void; onClose: () => void
}): JSX.Element {
  const [pick, setPick] = useState(value || people[0] || '')
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="canvas-dialog-form" onSubmit={(e) => { e.preventDefault(); if (pick) onSubmit(pick) }}>
        {people.length ? (
          <label className="canvas-field">
            <span>{t('gui.canvas.d_head')}</span>
            <select value={pick} onChange={(e) => setPick(e.target.value)}>
              {people.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
        ) : <p className="canvas-text canvas-faint">{t('gui.canvas.d_head_nobody')}</p>}
        <Buttons ok={t('gui.canvas.d_save')} onCancel={onClose} disabled={!pick} />
      </form>
    </Dialog>
  )
}
