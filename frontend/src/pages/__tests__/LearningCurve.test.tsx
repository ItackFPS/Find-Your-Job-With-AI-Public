// Quality › Personal ranking (ADR 0019, v5 L4.4): descriptive wording only.
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CurveReport } from '../LearningCurve'
import type { LearningCurve, LearningStatus } from '../../api/client'

const curve = (n: number, x: number, status: LearningCurve['status'] = 'measured') => ({
  status, positives: n, anchored: n, discordant: n, challenger_wins: x, production_wins: n - x,
  wording: { key: 'learning_curve.summary', n, x },
}) as LearningCurve

const status = {
  state: 'shadow', wording_key: 'learning.status.observing', activation_enabled: false, paused_notice: false,
  state_since: null, last_event: null,
  strata: { LB: { discordant: 4, challenger_wins: 3 }, PF: { discordant: 0, challenger_wins: 0 } },
} as LearningStatus

describe('CurveReport', () => {
  it('words the replay as "would have ranked higher in X cases"', () => {
    render(<CurveReport c={curve(12, 7)} />)
    expect(screen.getByTestId('curve-summary')).toHaveTextContent(
      'Of 12 offers you kept that the two rankings placed differently, the personal version would have ranked 7 higher.')
  })

  it('never says better or gives a probability', () => {
    const { container } = render(<CurveReport c={curve(12, 11)} status={status} />)
    expect(container.textContent).not.toMatch(/better|best|%|chance|probab/i)
  })

  it('says when there is nothing to measure', () => {
    render(<CurveReport c={curve(0, 0, 'no_positives')} />)
    expect(screen.getByText(/not enough choices/i)).toBeInTheDocument()
  })

  it('lists strata with discordant positives only', () => {
    render(<CurveReport status={status} />)
    expect(screen.getByText(/Discovery, sorted by date or distance\): 4 \/ 3/)).toBeInTheDocument()
    expect(screen.queryByText(/sorted by relevance/)).not.toBeInTheDocument()
  })
})
