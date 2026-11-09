import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { LearnedPreferences } from '../../components/settings/LearnedPreferences'
import * as client from '../../api/client'
import { ToastProvider } from '../../components'
import '../../i18n'

const base = {
  paused: false, signals: 5, strong_signals: 4, strong_needed: 3, enough_evidence: true, cap: 0.03,
  preferences: [{ feature: 'terms', value: 'reseau', weight: 0.6 }, { feature: 'terms', value: 'comptable', weight: -0.4 }],
  forgotten: [],
  comparison: {
    run_id: 'r', applied: true, ranked: 40, moved: 12, entered_top_20: 3,
    movers: [{ job_id: 'j', title: 'Technicien réseaux', company: 'A', production_position: 9, personal_position: 2, adjustment: 0.02, reasons: [] }],
  },
}

function renderPanel() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ToastProvider><LearnedPreferences /></ToastProvider>
    </QueryClientProvider>,
  )
}

describe('LearnedPreferences', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('shows what was learned and what it changed', async () => {
    vi.spyOn(client, 'getLearnedPreferences').mockResolvedValue(base)
    renderPanel()
    expect(await screen.findByText(/reseau/)).toBeInTheDocument()
    expect(screen.getByTestId('learning-comparison')).toHaveTextContent('12')
    expect(screen.getByText(/Technicien réseaux/)).toHaveTextContent('9')
  })

  it('forgets a preference', async () => {
    vi.spyOn(client, 'getLearnedPreferences').mockResolvedValue(base)
    const forget = vi.spyOn(client, 'forgetPreference').mockResolvedValue({ ...base, preferences: [] })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /reseau/ }))
    await waitFor(() => expect(forget).toHaveBeenCalled())
    expect(forget.mock.calls[0][0]).toMatchObject({ feature: 'terms', value: 'reseau' })
  })

  it('says how many clear choices are still needed', async () => {
    vi.spyOn(client, 'getLearnedPreferences').mockResolvedValue({ ...base, enough_evidence: false, strong_signals: 1, preferences: [], comparison: null })
    renderPanel()
    expect(await screen.findByTestId('learned-preferences')).toHaveTextContent('1')
  })
})
