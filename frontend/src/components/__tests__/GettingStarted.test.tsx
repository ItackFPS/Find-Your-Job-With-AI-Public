import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { GettingStarted } from '../GettingStarted'

function renderGuide() {
  return render(<MemoryRouter><GettingStarted /></MemoryRouter>)
}

describe('GettingStarted', () => {
  it('says nothing runs on its own', () => {
    renderGuide()
    expect(screen.getByText(/opening the app starts no scan and no run/)).toBeInTheDocument()
  })

  it('names the credentials each optional source needs', () => {
    renderGuide()
    expect(screen.getByText('FRANCE_TRAVAIL_CLIENT_ID and FRANCE_TRAVAIL_CLIENT_SECRET')).toBeInTheDocument()
    expect(screen.getByText('INDEED_API_KEY')).toBeInTheDocument()
  })

  it('links to the profile page', () => {
    renderGuide()
    expect(screen.getByRole('link', { name: 'Profile' })).toHaveAttribute('href', '/profile')
  })
})
