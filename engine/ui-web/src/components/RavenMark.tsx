/* The product mark: the Rarus globe (owner, 2026-10-19: "the globe like alembox's, dark yellow").
 *
 * A picture rather than an inline drawing: the globe is the Alem AI artwork recoloured by
 * tools/dev/make_icons.py, which also writes assets/raven-mark.svg (the splash, before any script
 * runs) and the program's icons from the same source, so all of them stay one picture. The file
 * is served with the page from its own folder, so it paints with the rail.
 *
 * No class names anywhere in it, so nothing here can collide with a page class
 * (scripts/check-class-namespace.mjs).
 */

import type { JSX } from 'react'

export const MARK_SRC = 'assets/rarus-mark.png'

export function RavenMark({ size = 24 }: { size?: number }): JSX.Element {
  return <img src={MARK_SRC} width={size} height={size} alt="" aria-hidden="true" decoding="async" draggable={false} />
}
