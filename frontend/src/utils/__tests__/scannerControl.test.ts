import { describe, it, expect } from 'vitest'
import {
  scannerControl, scannerProgressLabel, scannerState, scannerWorkEstimate, startHoldEnded, START_HOLD_SAFETY_MS,
} from '../scannerControl'

describe('startHoldEnded', () => {
  const hold = { fromScanId: 'scan-paused', since: 1_000 }

  it('does not end on the status cached from before the click', () => {
    expect(startHoldEnded(hold, { is_scanning: false, last_scan: { id: 'scan-paused' } }, 2_000)).toBe(false)
  })

  it('ends once the backend reports the scan running', () => {
    expect(startHoldEnded(hold, { is_scanning: true, last_scan: { id: 'scan-paused' } }, 2_000)).toBe(true)
  })

  it('ends when a new run already came and went between two polls', () => {
    expect(startHoldEnded(hold, { is_scanning: false, last_scan: { id: 'scan-new' } }, 2_000)).toBe(true)
  })

  it('is bounded by a safety net only if the start never materialises', () => {
    const later = hold.since + START_HOLD_SAFETY_MS + 1
    expect(startHoldEnded(hold, { is_scanning: false, last_scan: { id: 'scan-paused' } }, later)).toBe(true)
  })
})

describe('scannerControl', () => {
  it('offers Pause while running', () => {
    expect(scannerControl({ is_scanning: true, state: 'running' })).toEqual({
      state: 'running', action: 'pause', label: 'Pause', disabled: false,
    })
  })

  it('disables the control while the pause is being honoured', () => {
    const c = scannerControl({ is_scanning: true, state: 'pausing' })
    expect(c.action).toBe('none')
    expect(c.label).toBe('Pausing…')
    expect(c.disabled).toBe(true)
  })

  it.each(['paused', 'interrupted', 'rate_limited'] as const)('offers Resume when %s', (state) => {
    const c = scannerControl({ is_scanning: false, state })
    expect(c.action).toBe('resume')
    expect(c.label).toBe('Resume')
    expect(scannerControl({ is_scanning: false, state }, true).label).toBe('Resuming…')
  })

  it('offers Scan now when idle', () => {
    expect(scannerControl({ is_scanning: false, state: 'idle' }).action).toBe('scan')
    expect(scannerControl({ is_scanning: false, state: 'idle' }, true).label).toBe('Starting…')
  })

  it('shows a disabled Starting… until the backend is actually scanning', () => {
    expect(scannerControl({ is_scanning: false, state: 'paused' }, false, true)).toEqual({
      state: 'running', action: 'none', label: 'Starting…', disabled: true,
    })
  })

  it('falls back to is_scanning when the backend sends no state', () => {
    expect(scannerState({ is_scanning: true })).toBe('running')
    expect(scannerState({ is_scanning: false })).toBe('idle')
  })
})

describe('scannerProgressLabel', () => {
  it('describes LinkedIn progress as query and page', () => {
    expect(scannerProgressLabel({
      queries_completed: 3, queries_total: 12, current_query: 'data engineer',
      current_page: 6, updated_at: null,
    })).toBe('Query 4 / 12 · page 7')
  })

  it('omits the page between queries', () => {
    expect(scannerProgressLabel({
      queries_completed: 12, queries_total: 12, current_query: null,
      current_page: 0, updated_at: null,
    })).toBe('Query 12 / 12')
  })

  it('describes HelloWork progress as a count, never a percentage', () => {
    expect(scannerProgressLabel({ slugs_completed: 1, updated_at: null })).toBe('1 metier page done')
    expect(scannerProgressLabel({ slugs_completed: 42, updated_at: null })).toBe('42 metier pages done')
  })

  it('returns null without a checkpoint', () => {
    expect(scannerProgressLabel(null)).toBeNull()
    expect(scannerProgressLabel(undefined)).toBeNull()
  })
})

describe('scanner work estimate (issue #86)', () => {
  const linkedIn = {
    queries_completed: 6, queries_total: 12, current_query: 'devops', current_page: 6,
    updated_at: null, percent: 55, pages_done_est: 66, pages_total_est: 120,
    current_query_pages_est: 10, estimated: true, eta_seconds: 4_320, eta_basis: 'live' as const,
  }

  it('appends the estimated page totals to the LinkedIn label', () => {
    expect(scannerProgressLabel(linkedIn)).toBe('Query 7 / 12 · page 7 · ~66 / 120 pages')
  })

  it('exposes the backend percent and ETA for LinkedIn', () => {
    expect(scannerWorkEstimate(linkedIn)).toEqual({ percent: 55, etaSeconds: 4_320 })
    expect(scannerWorkEstimate({ ...linkedIn, eta_seconds: null })).toEqual({ percent: 55, etaSeconds: null })
  })

  it('has no work estimate for HelloWork or an older backend', () => {
    expect(scannerWorkEstimate({ slugs_completed: 3, updated_at: null })).toBeNull()
    expect(scannerWorkEstimate({ ...linkedIn, percent: undefined })).toBeNull()
    expect(scannerWorkEstimate(null)).toBeNull()
  })
})
