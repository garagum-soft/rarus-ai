/* A preview that follows its file: the gateway streams each rewrite of the file
 * open in the viewer (`/file/watch`, server-sent events) and the viewer counts
 * them. The count rides into the preview's `version`, so an agent saving the
 * document it is editing redraws the picture, the deck's pages or the PDF frame
 * in place -- no reload, and the rest of the page keeps its state. */

import { useEffect, useState } from 'react'

import { current as currentSession } from '../../lib/session'

export const watchURL = (path: string): string => {
  const session = currentSession()
  return '/file/watch?path=' + encodeURIComponent(path) + (session ? '&session=' + encodeURIComponent(session) : '')
}

/* How many times `path` has changed since it was opened; 0 while `active` is
   false or the browser has no EventSource. The stream closes with the view. */
export function useLiveFile(path: string, active: boolean): number {
  const [changes, setChanges] = useState(0)
  useEffect(() => {
    setChanges(0)
    if (!active || typeof EventSource === 'undefined') return undefined
    const stream = new EventSource(watchURL(path), { withCredentials: true })
    const bump = (): void => { setChanges((n) => n + 1) }
    stream.addEventListener('change', bump)
    return () => {
      stream.removeEventListener('change', bump)
      stream.close()
    }
  }, [path, active])
  return changes
}
