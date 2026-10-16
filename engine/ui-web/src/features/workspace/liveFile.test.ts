// @vitest-environment happy-dom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useLiveFile, watchURL } from './liveFile'

/* A stand-in for the browser's EventSource that a test can push events into. */
class FakeStream {
  static last: FakeStream | null = null
  static opened: string[] = []
  closed = false
  private listeners = new Map<string, Set<() => void>>()

  constructor(public url: string, public init?: EventSourceInit) {
    FakeStream.last = this
    FakeStream.opened.push(url)
  }

  addEventListener(name: string, fn: () => void): void {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set())
    this.listeners.get(name)!.add(fn)
  }

  removeEventListener(name: string, fn: () => void): void {
    this.listeners.get(name)?.delete(fn)
  }

  emit(name: string): void {
    this.listeners.get(name)?.forEach((fn) => fn())
  }

  close(): void {
    this.closed = true
  }
}

beforeEach(() => {
  FakeStream.last = null
  FakeStream.opened = []
  vi.stubGlobal('EventSource', FakeStream)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('a preview that follows its file', () => {
  it('asks the gateway to watch the file it shows', () => {
    expect(watchURL('reports/q3 deck.pptx')).toContain('/file/watch?path=reports%2Fq3%20deck.pptx')
  })

  it('counts every rewrite the stream reports, so the preview version moves', () => {
    const { result } = renderHook(() => useLiveFile('deck.pptx', true))
    expect(result.current).toBe(0)
    expect(FakeStream.last?.init).toEqual({ withCredentials: true })

    act(() => { FakeStream.last!.emit('change') })
    act(() => { FakeStream.last!.emit('change') })

    expect(result.current).toBe(2)
  })

  it('opens no stream for a view that does not preview, and closes the stream with the view', () => {
    const idle = renderHook(() => useLiveFile('notes.txt', false))
    expect(FakeStream.opened).toEqual([])
    idle.unmount()

    const open = renderHook(() => useLiveFile('deck.pptx', true))
    const stream = FakeStream.last!
    open.unmount()
    expect(stream.closed).toBe(true)
  })

  it('starts counting again for another file', () => {
    const view = renderHook(({ path }) => useLiveFile(path, true), { initialProps: { path: 'a.pdf' } })
    act(() => { FakeStream.last!.emit('change') })
    view.rerender({ path: 'b.pdf' })
    expect(view.result.current).toBe(0)
    expect(FakeStream.opened).toHaveLength(2)
  })
})
