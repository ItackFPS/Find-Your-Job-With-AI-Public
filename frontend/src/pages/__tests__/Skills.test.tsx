import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GapCard, SkillList } from '../Skills'
import type { SkillGraph } from '../../api/client'

const graph: SkillGraph = {
  window_days: 90, relevant_definition: 'x', n_jobs: 8, n_relevant: 4, owned: ['python'],
  nodes: [
    { skill: 'python', demand: 4, relevant_demand: 4, relevant_share: 1, owned: true },
    { skill: 'docker', demand: 3, relevant_demand: 3, relevant_share: 0.75, owned: false },
  ],
  edges: [{ source: 'docker', target: 'python', weight: 3 }],
  gaps: [{ skill: 'docker', relevant_demand: 3, demand: 3, relevant_share: 0.75,
    adjacent_owned: [{ skill: 'python', jobs: 3 }], example_titles: ['Dev Python 1'],
    why: 'asked for by 3 of 4 relevant job(s) (75%); often alongside your python' }],
  limitations: [],
}

describe('Skills', () => {
  it('shows a gap with its evidence', () => {
    render(<GapCard gap={graph.gaps[0]} />)
    expect(screen.getByText('docker')).toBeInTheDocument()
    expect(screen.getByText('75% of relevant jobs')).toBeInTheDocument()
    expect(screen.getByText(/often alongside your python/)).toBeInTheDocument()
    expect(screen.getByText(/Dev Python 1/)).toBeInTheDocument()
  })

  it('marks owned skills and lists related skills on selection', async () => {
    render(<SkillList graph={graph} />)
    expect(screen.getByText('yours')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /docker/ }))
    expect(screen.getByText('OFTEN ASKED WITH DOCKER')).toBeInTheDocument()
    expect(screen.getByText(/— 3 job\(s\)/)).toBeInTheDocument()
  })
})
