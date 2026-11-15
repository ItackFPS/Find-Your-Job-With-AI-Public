// Phase B B7-B: a finished run shows its real duration, the wall clock between its timestamps.
import { describe, it, expect } from 'vitest'
import { runWallSeconds } from '../formatDuration'

describe('runWallSeconds', () => {
  it('is completed_at - started_at, which covers the journal and saving too', () => {
    expect(runWallSeconds({ started_at: '2026-11-15T08:00:00Z', completed_at: '2026-11-15T08:03:25Z',
      stats: { duration_seconds: 82 } })).toBe(205)
  })

  it('falls back to the internal duration without both timestamps', () => {
    expect(runWallSeconds({ started_at: '2026-11-15T08:00:00Z', completed_at: null, stats: { duration_seconds: 82 } })).toBe(82)
    expect(runWallSeconds({ started_at: 'not a date', completed_at: 'x', stats: { duration_seconds: 5 } })).toBe(5)
    expect(runWallSeconds({})).toBeNull()
  })
})
