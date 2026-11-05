import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AgreementReport } from '../ExtractorAgreement'
import type { ExtractorAgreement } from '../../api/client'

const rate = (k: number, n: number) => ({ k, n, value: n ? k / n : null, ci95: n ? [0.5, 0.9] as [number, number] : null })

describe('AgreementReport', () => {
  it('shows ROME coverage, precision and recall per field, and beginner conflicts', () => {
    const a: ExtractorAgreement = {
      status: 'measured', offers: 120, limit: 2000, years_tolerance: 1, rome_coverage: rate(120, 120),
      experience: { precision: rate(40, 50), recall: rate(50, 80), disagreement: rate(10, 50), beginner_conflicts: 3 },
      education: { precision: rate(0, 0), recall: rate(0, 0), disagreement: rate(0, 0) },
    }
    render(<AgreementReport a={a} />)
    expect(screen.getByText('120 of 120 France Travail offers carry an occupation code (ROME): 100 %.')).toBeInTheDocument()
    expect(screen.getByText('80 % (40 of 50, likely between 50 % and 90 %)')).toBeInTheDocument()
    expect(screen.getAllByText('not enough offers')).toHaveLength(2)
    expect(screen.getByText(/read as asking for 2 years or more: 3/)).toBeInTheDocument()
  })

  it('says there is nothing to compare without France Travail offers', () => {
    render(<AgreementReport a={{ status: 'no_france_travail_offers', offers: 0, limit: 2000, years_tolerance: 1,
      rome_coverage: rate(0, 0), experience: { precision: rate(0, 0), recall: rate(0, 0), disagreement: rate(0, 0),
        beginner_conflicts: 0 }, education: { precision: rate(0, 0), recall: rate(0, 0), disagreement: rate(0, 0) } }} />)
    expect(screen.getByText(/No France Travail offer is stored yet/)).toBeInTheDocument()
  })
})
