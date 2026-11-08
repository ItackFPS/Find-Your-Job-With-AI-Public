import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { GettingStarted } from '../GettingStarted'

function renderGuide() {
  return render(<MemoryRouter><GettingStarted /></MemoryRouter>)
}

describe('GettingStarted', () => {
  it('says the daily search waits for a saved profile', () => {
    renderGuide()
    expect(screen.getByText(/Once you have saved your profile, the app searches by itself/)).toBeInTheDocument()
  })

  it('asks for no file edit and no credential', () => {
    const { container } = renderGuide()
    expect(container.textContent).not.toMatch(/\.env|FRANCE_TRAVAIL_CLIENT|credential/i)
  })

  it('states who this version is tuned for', () => {
    renderGuide()
    expect(screen.getByText(/tuned for IT and digital roles/)).toBeInTheDocument()
  })

  it('links to the profile page', () => {
    renderGuide()
    expect(screen.getByRole('link', { name: 'Profile' })).toHaveAttribute('href', '/profile')
  })
})
