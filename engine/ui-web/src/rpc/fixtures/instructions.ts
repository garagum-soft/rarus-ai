/* The instructions editor's files, held in memory for the offline page.
 *
 * Saves are checked against the hash the editor opened, like the gateway's
 * (a stale one answers revision_changed), and every save is a revision the
 * history can show and put back. The hash is the page's own stand-in, not a
 * sha256: the contract only asks for a string that changes with the text.
 */

import { RpcError } from '../transport'

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

interface Rev { rev: number; at: number; author: string; note: string; sha256: string; action: string; restored_from: number | null; text: string }
interface FixtureFile { key: string; kind: string; text: string | null; revs: Rev[] }

export interface InstructionsFixture {
  fixtures: Fixtures
}

/* FNV-1a, 32 bit, as hex: a deterministic fingerprint, enough to tell two texts apart here. */
function hash(text: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0')
}

const SEED: Array<[string, string, string | null]> = [
  ['agent_memory/profile/soul.md', 'instruction', '# Soul\n\nCalm, direct, a little dry. Says what it did, not what it might do.\n'],
  ['agent_memory/profile/agent.md', 'instruction', '# Agent\n\n- Read before writing.\n- Run the tests before calling it done.\n'],
  ['TOOLS.md', 'instruction', '# Tools\n\nPrefer the project\'s own scripts over ad-hoc commands.\n'],
  ['user_memory/profile/user.md', 'memory', '# User\n\nPrefers short answers. Works in Python and TypeScript.\n'],
  ['user_memory/attention.md', 'memory', '## User overrides\n- dnd: 22:30-07:00 reason=nighttime\n'],
  ['user_memory/behaviors.md', 'memory', null],
  ['HEARTBEAT.md', 'memory', null],
]

const changed = (current: string): RpcError =>
  new RpcError(-32023, 'revision_changed', { current_sha256: current, detail: 'the file changed since it was opened' })
const invalid = (detail: string): RpcError => new RpcError(-32011, 'config_validation_error', { detail })

export function createInstructions(env: FixtureEnv): InstructionsFixture {
  const at = (): number => env.now() / 1000
  /* Each file that exists starts with one revision, so the history has a row to show and put back. */
  const files = new Map<string, FixtureFile>(SEED.map(([key, kind, text]) => [key, {
    key, kind, text,
    revs: text === null ? [] : [{
      rev: 1, at: at(), author: 'baseline', note: 'the text before revisions were kept',
      sha256: hash(text), action: 'save', restored_from: null, text,
    }],
  }]))
  /* The soul has a past: an edit someone took back, which is the one row a
     history shows a rollback on. */
  const soul = files.get('agent_memory/profile/soul.md')!
  const tried = `${soul.text}Uses a lot of exclamation marks!\n`
  soul.revs.push(
    { rev: 2, at: at(), author: 'agent', note: 'write_file', sha256: hash(tried), action: 'save', restored_from: null, text: tried },
    { rev: 3, at: at(), author: 'web', note: 'roll back to revision 1', sha256: hash(soul.text!), action: 'rollback', restored_from: 1, text: soul.text! },
  )
  const find = (key: string): FixtureFile => {
    const f = files.get(key)
    if (!f) throw invalid(`${key} is not an editable file`)
    return f
  }
  const textOf = (f: FixtureFile): string => f.text ?? ''
  const record = (f: FixtureFile, author: string, note: string, action = 'save', restored: number | null = null): number => {
    const rev = (f.revs.at(-1)?.rev ?? 0) + 1
    f.revs.push({ rev, at: at(), author, note, sha256: hash(textOf(f)), action, restored_from: restored, text: textOf(f) })
    return rev
  }
  const head = (f: FixtureFile): number => f.revs.at(-1)?.rev ?? 0
  const view = (f: FixtureFile) => ({
    key: f.key, kind: f.kind, text: textOf(f), sha256: hash(textOf(f)), head: head(f),
    revisions: [...f.revs].reverse().slice(0, 30).map(({ text: _text, ...r }) => r),
  })
  const write = (f: FixtureFile, text: string, expected: string, author: string, note: string, action = 'save', restored: number | null = null) => {
    if (hash(textOf(f)) !== expected) throw changed(hash(textOf(f)))
    if (textOf(f) === text && action === 'save') return { key: f.key, sha256: hash(text), head: head(f), saved: false }
    if (f.revs.length === 0 && f.text !== null) record(f, 'baseline', 'the text before revisions were kept')
    f.text = text
    record(f, author, note, action, restored)
    return { key: f.key, sha256: hash(text), head: head(f), saved: true }
  }
  return {
    fixtures: {
      'instructions.list': () => ({
        files: [...files.values()].map((f) => ({
          key: f.key, kind: f.kind, exists: f.text !== null, size: textOf(f).length,
          sha256: hash(textOf(f)), head: head(f), revisions: f.revs.length,
        })),
      }),
      'instructions.get': (p) => view(find((p as { key: string }).key)),
      'instructions.save': (p) => {
        const { key, text, expected_sha256: expected, note } = p as { key: string; text: string; expected_sha256: string; note?: string }
        return write(find(key), text, expected, 'web', note || '')
      },
      'instructions.revision': (p) => {
        const { key, rev } = p as { key: string; rev: number }
        const found = find(key).revs.find((r) => r.rev === rev)
        if (!found) throw invalid(`${key} has no revision ${rev}`)
        return { key, rev, text: found.text }
      },
      'instructions.diff': (p) => {
        const { key, a } = p as { key: string; a: number }
        return { key, diff: `--- ${key}@${a}\n+++ ${key}@${head(find(key))}\n` }
      },
      'instructions.rollback': (p) => {
        const { key, rev, expected_sha256: expected } = p as { key: string; rev: number; expected_sha256: string }
        const f = find(key)
        const old = f.revs.find((r) => r.rev === rev)
        if (!old) throw invalid(`${key} has no revision ${rev}`)
        return write(f, old.text, expected, 'web', `roll back to revision ${rev}`, 'rollback', rev)
      },
    },
  }
}
