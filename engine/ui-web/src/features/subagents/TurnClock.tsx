/* How long the turn this instance is answering has been running, and how long
 * it has been quiet.
 *
 * The same number the main transcript puts beside a running card, in the same
 * spelling (`lib/duration.ts`) and on the same self-stopping clock
 * (`lib/tick.ts`), so a reader who has learned to read one has learned both.
 *
 * The instance's own lifetime would have been the easy number and the wrong
 * one: it is a different fact wearing the same clothes, and a pane header is
 * exactly where the two would be confused. What the server publishes is the
 * turn's start and only while there is a turn -- `turnStartedAtMs` absent is
 * "answering nothing", which is why this draws nothing rather than freezing on
 * a last value when the run ends.
 *
 * Not `updatedAtMs`, which was the tempting field already on the row: every
 * registry write stamps it, so a clock counting from it would jump whenever a
 * binding commit or a graph-origin write touched the record.
 *
 * "Working for 14m" alone says the same thing about a run that is busy and one
 * that hung on its first call. `lastSignalAtMs` is the run's last sign of life
 * (a tool call, a step, output) as the server saw it, so the gap since then is
 * shown once it is long enough to mean something: quiet from a minute, and from
 * ten minutes -- the same line the graph runner tells the main agent at -- as
 * no sign of life. Neither is a failure, and the titles say so.
 */

import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'
import { useTick } from '../../lib/tick'

import type { InstanceRow } from './types'
import type { JSX } from 'react'

export const QUIET_MS = 60_000
export const STALLED_MS = 600_000

export function TurnClock({ row }: { row: InstanceRow }): JSX.Element | null {
  const began = row.turnStartedAtMs
  const live = typeof began === 'number' && began > 0
  /* Called before the early return, or the hook count changes with the row's
     state and React unmounts the pane around it. `0` is never read when the
     clock is off. */
  const elapsed = useTick(live, live ? began : 0)
  if (!live) return null
  /* Held back below a second, the way the transcript holds its own: a clock
     that opens on "0.0s" reads as broken rather than as new. */
  if (elapsed < 1000) return null
  const spelled = formatDuration(elapsed)
  /* Only from a server that publishes it: without the field nothing is known
     about the run's signs of life, and guessing from the turn's start would call
     every long turn quiet. */
  const known = typeof row.lastSignalAtMs === 'number'
  const signal = known && (row.lastSignalAtMs as number) >= began ? (row.lastSignalAtMs as number) : began
  const quiet = known ? began + elapsed - signal : 0
  const stalled = quiet >= STALLED_MS
  const shownQuiet = formatDuration(quiet)
  return (
    <>
      <span className="pane-turnms" title={t('gui.ws.instance_turn_elapsed', { d: spelled })}>
        {spelled}
      </span>
      {quiet >= QUIET_MS ? (
        <span
          className={`subagents-quiet${stalled ? ' subagents-quiet-stalled' : ''}`}
          title={t(stalled ? 'gui.ws.instance_stalled_title' : 'gui.ws.instance_quiet_title', { d: shownQuiet })}
        >
          {t(stalled ? 'gui.ws.instance_stalled' : 'gui.ws.instance_quiet', { d: shownQuiet })}
        </span>
      ) : null}
    </>
  )
}
