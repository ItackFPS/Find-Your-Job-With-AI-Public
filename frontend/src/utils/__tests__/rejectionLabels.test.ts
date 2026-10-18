import { describe, it, expect } from 'vitest'
import { rejectionReasonLabel } from '../rejectionLabels'

// Canonical codes mirror src/pipeline/filter.py's _FILTER_CHECKS and
// src/requirements/models.py's HardRejectionReason.type — the backend keeps
// sending these unchanged; only the display label is presentation-only.
describe('rejectionReasonLabel', () => {
  it('maps known filter-stage codes to human-readable labels', () => {
    expect(rejectionReasonLabel('title_keywords')).toBe('Title keywords')
    expect(rejectionReasonLabel('seniority')).toBe('Seniority')
    expect(rejectionReasonLabel('published_age')).toBe('Published age')
    expect(rejectionReasonLabel('location')).toBe('Location')
  })

  it('maps known requirements-gate codes to human-readable labels', () => {
    expect(rejectionReasonLabel('experience')).toBe('Experience')
    expect(rejectionReasonLabel('education')).toBe('Education')
    expect(rejectionReasonLabel('principal_skill')).toBe('Principal skill')
  })

  it('never includes a stage suffix like "(filter)" or "(requirements gate)"', () => {
    for (const code of ['title_keywords', 'seniority', 'published_age', 'experience', 'education', 'principal_skill', 'location']) {
      const label = rejectionReasonLabel(code)
      expect(label).not.toMatch(/\(/)
      expect(label).not.toMatch(/filter|requirements gate/i)
    }
  })

  it('falls back to a generic title-cased rendering for an unknown code', () => {
    expect(rejectionReasonLabel('some_new_reason')).toBe('Some New Reason')
  })
})
