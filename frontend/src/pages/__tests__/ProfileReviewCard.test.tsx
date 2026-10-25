import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { profile as baseProfile } from '../../test/apiFixtures'
import type { Profile } from '../../api/client'
import { ProfileReviewCard } from '../ProfileReviewCard'

const migrated: Profile = {
  ...baseProfile,
  profile_version: 2,
  targets: { levels: ['graduate', 'junior'], stretch: false, years_override: null },
  education: [{ id: 'edu-1', qualification: 'bac+3', level: 6, field: null, institution: null, year: null,
    in_progress: false, foreign: false }],
  experiences: [],
  review: { pending: true, notes: ['maximum distance is the 150 km cap', 'excluded départements kept as exclusions: 13'],
    migrated_at: '2026-10-25T00:00:00+00:00' },
}

describe('ProfileReviewCard', () => {
  it('lists the converted levels and every note, and confirms once', async () => {
    const onConfirm = vi.fn()
    render(<ProfileReviewCard profile={migrated} onConfirm={onConfirm} saving={false} />)
    expect(screen.getByText('Graduate / entry level, Junior')).toBeInTheDocument()
    expect(screen.getByText('level 6')).toBeInTheDocument()
    expect(screen.getByText('maximum distance is the 150 km cap')).toBeInTheDocument()
    expect(screen.getByText('excluded départements kept as exclusions: 13')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Looks right' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('renders nothing once reviewed or for a profile never migrated', () => {
    const { container, rerender } = render(
      <ProfileReviewCard profile={{ ...migrated, review: { ...migrated.review!, pending: false } }} onConfirm={() => {}} saving={false} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<ProfileReviewCard profile={baseProfile} onConfirm={() => {}} saving={false} />)
    expect(container).toBeEmptyDOMElement()
  })
})
