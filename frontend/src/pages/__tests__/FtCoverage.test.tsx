import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CoverageSentence } from '../FtCoverage'

describe('CoverageSentence', () => {
  it('states the share, its interval and that a short window is not final', () => {
    render(<CoverageSentence c={{ status: 'too_early', window_days: 7, days_with_both: 3, api_offers: 200,
      web_offers: 180, found_by_web: 150, share: 0.75, ci95: [0.69, 0.81] }} />)
    expect(screen.getByText('The website reader found 150 of the 200 API offers (75 %, likely between 69 % and 81 %).')).toBeInTheDocument()
    expect(screen.getByText('Both sources ran on 3 of the 7 days needed: this figure is not final yet.')).toBeInTheDocument()
  })

  it('says why there is nothing to compare without the API', () => {
    render(<CoverageSentence c={{ status: 'no_api_offers', window_days: 7, days_with_both: 0, api_offers: 0,
      web_offers: 40, found_by_web: 0, share: null, ci95: null }} />)
    expect(screen.queryByText(/website reader found/)).toBeNull()
    expect(screen.getByText(/needs credentials/)).toBeInTheDocument()
  })
})
