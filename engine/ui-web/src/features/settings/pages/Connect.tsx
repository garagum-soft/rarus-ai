/* Connect an agent: pick the client an outside agent runs in, and the page
   gives the one thing that client needs to reach Raven's MCP server -- a
   command to run, or a file to write -- and the text to brief the agent with
   once it is connected. Both come from the gateway (settings.mcpClients), so
   the command names this install's own launcher and the brief names the tools
   the server really has. */
import { useEffect } from 'react'

import { t } from '../../../i18n/t'
import { copy } from '../../../lib/clipboard'
import { Card, Empty, GLYPH, Row, Seg } from '../Fields'
import * as store from '../store'

import type { McpClient } from '../types'
import type { JSX } from 'react'

/** The client the page shows: the one picked, else the first. */
export function chosen(clients: McpClient[], picked: string | null): McpClient | null {
  return clients.find((c) => c.client === picked) || clients[0] || null
}

function Block({ text, label }: { text: string; label: string }): JSX.Element {
  return (
    <div className="settings-connect-block">
      <pre className="settings-connect-pre">{text}</pre>
      <button type="button" className="mini ghost" onClick={() => copy(text, t('gui.settings.connect.copied'))}>
        {label}
      </button>
    </div>
  )
}

export function Connect(): JSX.Element {
  const s = store.get()
  useEffect(() => {
    if (s.clients === undefined) void store.clientsLoad()
  }, [s.clients])
  if (s.clients === undefined) {
    return <Card><Row label={t('gui.settings.connect.client')}>{t('gui.settings.connect.loading')}</Row></Card>
  }
  if (s.clients === null || s.clients.length === 0) {
    return (
      <Card>
        <Empty
          icon={GLYPH.plug}
          title={t('gui.settings.connect.none')}
          action={{ label: t('gui.plug.retry'), onClick: () => store.set({ clients: undefined }) }}
        />
      </Card>
    )
  }
  const pick = chosen(s.clients, s.client)!
  const where = pick.kind === 'config'
    ? t('gui.settings.connect.file', { file: pick.filename })
    : t('gui.settings.connect.run')
  return (
    <Card>
      <Row label={t('gui.settings.connect.client')} stack>
        <span className="settings-connect-pick">
          <Seg
            opts={s.clients.map((c): [string, string] => [c.client, c.label])}
            value={pick.client}
            onPick={(client) => store.set({ client })}
          />
        </span>
      </Row>
      <Row label={where} stack>
        <Block text={pick.connect} label={t('gui.settings.connect.copy')} />
      </Row>
      <Row label={t('gui.settings.connect.instructions')} sub={t('gui.settings.connect.instructions_sub')} stack>
        <Block text={pick.instructions} label={t('gui.settings.connect.copy')} />
      </Row>
    </Card>
  )
}
