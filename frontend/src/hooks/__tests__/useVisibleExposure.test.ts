import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ExposureTracker, FLUSH_MS, MIN_VISIBLE_MS } from '../useVisibleExposure'
import type { ExposureItem } from '../../api/client'

describe('ExposureTracker', () => {
  let visible = true
  let sent: ExposureItem[][] = []
  let tracker: ExposureTracker
  let el: Element

  beforeEach(() => {
    vi.useFakeTimers()
    visible = true
    sent = []
    tracker = new ExposureTracker({ surface: 'jobs', send: items => sent.push(items), isTabVisible: () => visible })
    el = document.createElement('div')
    tracker.observe(el, 'j1', 3)
  })

  afterEach(() => {
    tracker.dispose()
    vi.useRealTimers()
  })

  it('logs a card at least half visible for one second', () => {
    tracker.handle([{ target: el, intersectionRatio: 0.6 }])
    vi.advanceTimersByTime(MIN_VISIBLE_MS)
    tracker.flush()
    expect(sent).toHaveLength(1)
    expect(sent[0][0]).toMatchObject({ job_id: 'j1', surface: 'jobs', position: 3 })
  })

  it('does not log under one second', () => {
    tracker.handle([{ target: el, intersectionRatio: 0.9 }])
    vi.advanceTimersByTime(MIN_VISIBLE_MS - 100)
    tracker.handle([{ target: el, intersectionRatio: 0.1 }])
    vi.advanceTimersByTime(MIN_VISIBLE_MS)
    tracker.flush()
    expect(sent).toEqual([])
  })

  it('does not log a card less than half visible', () => {
    tracker.handle([{ target: el, intersectionRatio: 0.4 }])
    vi.advanceTimersByTime(MIN_VISIBLE_MS * 3)
    tracker.flush()
    expect(sent).toEqual([])
  })

  it('does not log while the tab is hidden', () => {
    tracker.handle([{ target: el, intersectionRatio: 1 }])
    vi.advanceTimersByTime(500)
    visible = false
    tracker.visibilityChanged()
    vi.advanceTimersByTime(MIN_VISIBLE_MS * 3)
    tracker.flush()
    expect(sent).toEqual([])
    // back to visible: the clock restarts from zero
    visible = true
    tracker.visibilityChanged()
    vi.advanceTimersByTime(MIN_VISIBLE_MS)
    tracker.flush()
    expect(sent).toHaveLength(1)
  })

  it('logs an offer once per list and batches on the flush interval', () => {
    tracker.handle([{ target: el, intersectionRatio: 1 }])
    vi.advanceTimersByTime(MIN_VISIBLE_MS)
    tracker.handle([{ target: el, intersectionRatio: 0 }])
    tracker.handle([{ target: el, intersectionRatio: 1 }])
    vi.advanceTimersByTime(FLUSH_MS)
    expect(sent).toHaveLength(1)
    expect(sent[0]).toHaveLength(1)
  })

  it('never logs a card that is only rendered', () => {
    vi.advanceTimersByTime(FLUSH_MS * 2)
    expect(sent).toEqual([])
  })
})

describe('ExposureTracker on Découverte', () => {
  it('marks Découverte exposures as randomized', () => {
    vi.useFakeTimers()
    const sent: ExposureItem[][] = []
    const tracker = new ExposureTracker({
      surface: 'decouverte', randomized: true, send: items => sent.push(items), isTabVisible: () => true,
    })
    const el = document.createElement('div')
    tracker.observe(el, 'j9', 0)
    tracker.handle([{ target: el, intersectionRatio: 1 }])
    vi.advanceTimersByTime(MIN_VISIBLE_MS)
    tracker.flush()
    expect(sent[0][0]).toMatchObject({ job_id: 'j9', surface: 'decouverte', position: 0, randomized: true })
    tracker.dispose()
    vi.useRealTimers()
  })
})
