// Visible exposure (ADR 0016): an offer counts as seen when at least half its card stays in the
// viewport for at least one second while the tab is visible. A rendered card is never logged.
// One shared IntersectionObserver per list; observations are batched and posted best-effort.

import { useEffect, useRef } from 'react'
import { postExposureBatch, type ExposureItem } from '../api/client'
import { getGenerationToken } from '../api/generation'

export const MIN_RATIO = 0.5
export const MIN_VISIBLE_MS = 1000
export const FLUSH_MS = 5000

interface Tracked {
  jobId: string
  position: number
  inView: boolean
  since: number | null
  timer: ReturnType<typeof setTimeout> | null
}

export interface TrackerOptions {
  surface: string
  send: (items: ExposureItem[]) => void
  now?: () => number
  isTabVisible?: () => boolean
  sortKey?: string
  /** Découverte (S4) slots come from a random draw independent of every ranking (ADR 0019). */
  randomized?: boolean
}

export interface IntersectionLike {
  target: Element
  intersectionRatio: number
}

export class ExposureTracker {
  private tracked = new Map<Element, Tracked>()
  private logged = new Set<string>()
  private queue: ExposureItem[] = []
  private observer: IntersectionObserver | null = null
  private flushTimer: ReturnType<typeof setInterval> | null = null
  private readonly now: () => number
  private readonly isTabVisible: () => boolean
  private readonly opts: TrackerOptions

  constructor(opts: TrackerOptions) {
    this.opts = opts
    this.now = opts.now ?? (() => Date.now())
    this.isTabVisible = opts.isTabVisible ?? (() => typeof document === 'undefined' || document.visibilityState === 'visible')
    if (typeof IntersectionObserver !== 'undefined') {
      this.observer = new IntersectionObserver(entries => this.handle(entries), { threshold: [0, MIN_RATIO, 1] })
    }
    this.flushTimer = setInterval(() => this.flush(), FLUSH_MS)
  }

  observe(el: Element, jobId: string, position: number): () => void {
    if (!this.tracked.has(el)) {
      this.tracked.set(el, { jobId, position, inView: false, since: null, timer: null })
      this.observer?.observe(el)
    }
    return () => this.unobserve(el)
  }

  unobserve(el: Element): void {
    const t = this.tracked.get(el)
    if (!t) return
    this.stop(t)
    this.tracked.delete(el)
    this.observer?.unobserve(el)
  }

  /** IntersectionObserver callback (public for tests). */
  handle(entries: IntersectionLike[]): void {
    for (const e of entries) {
      const t = this.tracked.get(e.target)
      if (!t) continue
      t.inView = e.intersectionRatio >= MIN_RATIO
      if (t.inView) this.start(t)
      else this.stop(t)
    }
  }

  /** Tab visibility changed: hidden stops every clock; visible restarts the cards in view. */
  visibilityChanged(): void {
    const visible = this.isTabVisible()
    for (const t of this.tracked.values()) {
      if (!visible) this.stop(t)
      else if (t.inView) this.start(t)
    }
  }

  flush(): void {
    if (!this.queue.length) return
    const items = this.queue
    this.queue = []
    this.opts.send(items)
  }

  dispose(): void {
    this.flush()
    for (const t of this.tracked.values()) this.stop(t)
    this.tracked.clear()
    this.observer?.disconnect()
    if (this.flushTimer) clearInterval(this.flushTimer)
  }

  private start(t: Tracked) {
    if (t.since !== null || this.logged.has(t.jobId) || !this.isTabVisible()) return
    t.since = this.now()
    t.timer = setTimeout(() => {
      t.timer = null
      if (t.since === null || !t.inView || !this.isTabVisible()) return
      this.logged.add(t.jobId)
      this.queue.push({
        job_id: t.jobId,
        surface: this.opts.surface,
        position: t.position,
        visible_ms: Math.max(MIN_VISIBLE_MS, this.now() - t.since),
        sort_key: this.opts.sortKey ?? null,
        randomized: this.opts.randomized ?? false,
        client_created_at: new Date().toISOString(),
      })
      t.since = null
    }, MIN_VISIBLE_MS)
  }

  private stop(t: Tracked) {
    if (t.timer) clearTimeout(t.timer)
    t.timer = null
    t.since = null
  }
}

const SESSION_ID = (() => {
  const c = globalThis.crypto
  return c && typeof c.randomUUID === 'function' ? c.randomUUID() : `s-${Date.now().toString(36)}`
})()

export function sendExposure(items: ExposureItem[]): void {
  void postExposureBatch(getGenerationToken(), SESSION_ID, items)
}

/** Returns a stable ref-callback factory: `ref={track(job.id, index)}`. */
export function useVisibleExposure(surface: string, sortKey?: string, randomized = false) {
  const trackerRef = useRef<ExposureTracker | null>(null)
  const callbacks = useRef(new Map<string, (el: Element | null) => void>())
  const elements = useRef(new Map<string, Element>())

  useEffect(() => {
    const tracker = new ExposureTracker({ surface, sortKey, randomized, send: sendExposure })
    trackerRef.current = tracker
    for (const [key, el] of elements.current) {
      const [jobId, pos] = splitKey(key)
      tracker.observe(el, jobId, pos)
    }
    const onVisibility = () => tracker.visibilityChanged()
    const onHide = () => tracker.flush()
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', onHide)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', onHide)
      tracker.dispose()
      trackerRef.current = null
    }
  }, [surface, sortKey, randomized])

  return (jobId: string, position: number) => {
    const key = `${position}\u0000${jobId}`
    let cb = callbacks.current.get(key)
    if (!cb) {
      cb = (el: Element | null) => {
        const prev = elements.current.get(key)
        if (prev && prev !== el) {
          trackerRef.current?.unobserve(prev)
          elements.current.delete(key)
        }
        if (el) {
          elements.current.set(key, el)
          trackerRef.current?.observe(el, jobId, position)
        }
      }
      callbacks.current.set(key, cb)
    }
    return cb
  }
}

function splitKey(key: string): [string, number] {
  const i = key.indexOf('\u0000')
  return [key.slice(i + 1), Number(key.slice(0, i))]
}
