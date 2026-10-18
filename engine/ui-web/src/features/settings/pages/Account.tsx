/* Account: who is signed in to Rarus and what is left of their credits. Read from `rarus.account`
   on every visit and by the refresh button; the server owns the balance and takes credits off
   itself (spec 18), so the page only shows it. Topping up happens on the site, and signing out is
   the desktop window's own action -- an ordinary browser has no window to send back to the sign-in
   page, so there the row says where to do it. */
import { useEffect, useState } from 'react'

import { t } from '../../../i18n/t'
import { open as openUrl } from '../../../lib/openUrl'
import { shell } from '../../../lib/shell'
import { show as toast } from '../../../state/toast'
import { Card, InlineErr, Row, Rov } from '../Fields'
import * as store from '../store'
import { UpdateRow } from './Updates'

import type { AccountInfo } from '../types'
import type { JSX } from 'react'

/* Where credits are topped up: the balance and packs page on the Rarus site. */
export const TOP_UP_URL = 'https://rarus-ai.com/pricing'

/* The codes `rarus.account` answers with (raven/providers/rarus_client.py), each in words; a text
   that is not one of them is shown as it came. */
export function errorText(code: string): string {
  switch (code) {
    case 'unauthorized': return t('gui.settings.account.err_unauthorized')
    case 'no_credits': return t('gui.settings.account.err_no_credits')
    case 'blocked': return t('gui.settings.account.err_blocked')
    case 'network': return t('gui.settings.account.err_network')
    case 'server': return t('gui.settings.account.err_server')
    default: return code
  }
}

export function balanceText(a: AccountInfo): string {
  return typeof a.balance === 'number' ? t('gui.settings.account.credits', { n: a.balance.toLocaleString() }) : t('gui.settings.account.balance_unknown')
}

export function Account(): JSX.Element {
  const [info, setInfo] = useState<AccountInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [windowed, setWindowed] = useState<boolean | null>(null)

  const load = async (): Promise<void> => {
    setBusy(true)
    try {
      setInfo(await store.source().account())
    } catch (e) {
      setInfo({ signed_in: false, email: '', balance: null, error: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    void load()
    void shell().then((api) => setWindowed(!!api))
  }, [])

  const signOut = async (): Promise<void> => {
    const api = await shell()
    if (!api) {
      toast(t('gui.settings.account.sign_out_hint'))
      return
    }
    try {
      await api.sign_out()
    } catch {
      toast(t('gui.settings.account.sign_out_failed'))
    }
  }

  return (
    <Card title={t('gui.settings.account.title')}>
      <Row label={t('gui.settings.account.email')}>
        <Rov>{info ? (info.signed_in ? info.email : t('gui.settings.account.signed_out')) : t('gui.settings.account.loading')}</Rov>
      </Row>
      <Row label={t('gui.settings.account.balance')} sub={t('gui.settings.account.balance_sub')}>
        <Rov>{info && info.signed_in ? balanceText(info) : '—'}</Rov>
        <button type="button" className="mini ghost" disabled={busy} onClick={() => void load()}>
          {t('gui.settings.account.refresh')}
        </button>
      </Row>
      <Row label={t('gui.settings.account.top_up')} sub={t('gui.settings.account.top_up_sub')}>
        <button type="button" className="mini ghost" onClick={() => openUrl(TOP_UP_URL)}>
          {t('gui.settings.account.top_up_open')}
        </button>
      </Row>
      <UpdateRow windowed={windowed} />
      <Row label={t('gui.settings.account.sign_out')} sub={windowed === false ? t('gui.settings.account.sign_out_hint') : undefined}>
        <button type="button" className="mini ghost" disabled={windowed === false} onClick={() => void signOut()}>
          {t('gui.settings.account.sign_out_do')}
        </button>
      </Row>
      {info && info.error ? <InlineErr text={errorText(info.error)} /> : null}
    </Card>
  )
}
