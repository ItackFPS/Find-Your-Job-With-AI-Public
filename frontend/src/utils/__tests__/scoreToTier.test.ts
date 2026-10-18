import { describe, it, expect } from 'vitest'
import { scoreToTier } from '../scoreToTier'

// Mirrors the exact thresholds in src/pipeline/scoring.py's
// score_to_tier / _TIER_THRESHOLDS — do not invent different thresholds
// here. Phase 4 (2026-09-10): fixed-reference calibration, see the
// calibration methodology documented at src/pipeline/scoring.py.
describe('scoreToTier', () => {
  it('classifies >=80 as exceptional', () => {
    expect(scoreToTier(80)).toBe('exceptional')
    expect(scoreToTier(100)).toBe('exceptional')
  })

  it('classifies >=72 and <80 as excellent', () => {
    expect(scoreToTier(72)).toBe('excellent')
    expect(scoreToTier(79)).toBe('excellent')
  })

  it('classifies >=65 and <72 as strong', () => {
    expect(scoreToTier(65)).toBe('strong')
    expect(scoreToTier(71)).toBe('strong')
  })

  it('classifies >=60 and <65 as relevant', () => {
    expect(scoreToTier(60)).toBe('relevant')
    expect(scoreToTier(64)).toBe('relevant')
  })

  it('classifies <60 as low_priority', () => {
    expect(scoreToTier(59)).toBe('low_priority')
    expect(scoreToTier(0)).toBe('low_priority')
  })

  it('classifies just-below-boundary values into the lower tier', () => {
    expect(scoreToTier(79)).toBe('excellent')
    expect(scoreToTier(71)).toBe('strong')
    expect(scoreToTier(64)).toBe('relevant')
    expect(scoreToTier(59)).toBe('low_priority')
  })

  it('produces monotonically non-decreasing tier rank as score increases', () => {
    const rank: Record<string, number> = {
      low_priority: 0, relevant: 1, strong: 2, excellent: 3, exceptional: 4,
    }
    let prevRank = -1
    for (let s = 0; s <= 100; s++) {
      const r = rank[scoreToTier(s)]
      expect(r).toBeGreaterThanOrEqual(prevRank)
      prevRank = r
    }
  })
})
