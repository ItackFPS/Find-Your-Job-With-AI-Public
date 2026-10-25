// Issue #81 review (F5): after Resume is accepted, the scanner card must stay
// a disabled "Starting…" while the status still reports the paused scan (the
// cached / not-yet-updated response), and switch to Pause only once the
// backend confirms the scan is running.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { ScannerStatusCard } from '../Dashboard'
import { START_HOLD_SAFETY_MS } from '../../utils/scannerControl'

const pausedStatus = {
  enabled: true,
  is_scanning: false,
  state: 'paused' as const,
  progress: null,
  last_scan: {
    id: 'scan-paused', status: 'paused', started_at: '2026-09-26T08:00:00Z',
    completed_at: '2026-09-26T08:05:00Z', jobs_found: 0, jobs_new: 0,
  },
}

function card(status: typeof pausedStatus | Record<string, unknown>, resumeFn: () => Promise<unknown>): ReactElement {
  return (
    <ScannerStatusCard
      name="LinkedIn scan"
      queryKey="linkedin-status"
      triggerFn={vi.fn()}
      pauseFn={vi.fn()}
      resumeFn={resumeFn}
      status={status as never}
      avgDurationSeconds={null}
    />
  )
}

describe('ScannerStatusCard — Resume', () => {
  it('holds a disabled Starting… until the backend reports the scan running', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const resumeFn = vi.fn().mockResolvedValue({ scan_id: null, exec_id: 'exec-1' })
    const wrap = (el: ReactElement) => <QueryClientProvider client={client}>{el}</QueryClientProvider>

    const { rerender } = render(wrap(card(pausedStatus, resumeFn)))
    await userEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(resumeFn).toHaveBeenCalledTimes(1)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled())

    // A refetch still returning the paused scan must not end the hold.
    rerender(wrap(card({ ...pausedStatus }, resumeFn)))
    expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()

    // Backend confirmation: the scan is running → Pause.
    rerender(wrap(card({
      ...pausedStatus, is_scanning: true, state: 'running',
      last_scan: { ...pausedStatus.last_scan, id: 'scan-resumed', status: 'running', completed_at: null },
    }, resumeFn)))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
  })
})

// Fake only the card's 1 s tick (setInterval) and its clock (Date); React
// Query and waitFor keep their real setTimeout, so nothing waits for real.
describe('ScannerStatusCard — Starting… hold timing', () => {
  const T0 = new Date('2026-09-26T09:00:00Z').getTime()
  const ETA_TEXT = /remaining \(estimated\)|Taking longer than usual|Finishing up/

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    vi.setSystemTime(T0)
  })
  afterEach(() => vi.useRealTimers())

  async function clickResume(status: typeof pausedStatus, avgDurationSeconds: number | null = null) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const resumeFn = vi.fn().mockResolvedValue({ scan_id: null, exec_id: 'exec-1' })
    const wrap = (s: Record<string, unknown>) => (
      <QueryClientProvider client={client}>
        <ScannerStatusCard
          name="LinkedIn scan" queryKey="linkedin-status" triggerFn={vi.fn()} pauseFn={vi.fn()}
          resumeFn={resumeFn} status={s as never} avgDurationSeconds={avgDurationSeconds}
        />
      </QueryClientProvider>
    )
    const { rerender } = render(wrap(status))
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled())
    return (s: Record<string, unknown>) => rerender(wrap(s))
  }

  it('holds Starting… while unconfirmed, then releases it after the 60 s safety bound', async () => {
    const rerender = await clickResume(pausedStatus)

    act(() => { vi.advanceTimersByTime(START_HOLD_SAFETY_MS - 5_000) })
    rerender({ ...pausedStatus })  // a poll still reporting the paused scan
    expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled()

    act(() => { vi.advanceTimersByTime(10_000) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled())
  })

  it("shows no ETA from the previous scan during Starting…, and the normal one once running", async () => {
    // The paused scan started 10 min ago; past runs took 5 min on average.
    const oldScan = { ...pausedStatus.last_scan, started_at: new Date(T0 - 600_000).toISOString() }
    const rerender = await clickResume({ ...pausedStatus, last_scan: oldScan }, 300)

    act(() => { vi.advanceTimersByTime(2_000) })
    expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled()
    expect(screen.queryByText(ETA_TEXT)).toBeNull()
    expect(screen.queryByText(/^~\d+%$/)).toBeNull()

    // The backend confirms the new scan, started 1 min ago → normal estimate.
    rerender({
      ...pausedStatus, is_scanning: true, state: 'running',
      last_scan: {
        ...oldScan, id: 'scan-resumed', status: 'running', completed_at: null,
        started_at: new Date(Date.now() - 60_000).toISOString(),
      },
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled())
    expect(screen.getByText(/remaining \(estimated\)/)).toBeInTheDocument()
    expect(screen.getByText('~20%')).toBeInTheDocument()  // 60 s of a 300 s average
  })
})

// Issue #86: LinkedIn's bar and ETA come from the backend's checkpoint-based
// estimate, so a resume (new started_at a few seconds ago) keeps them.
describe('ScannerStatusCard — work-based LinkedIn progress', () => {
  const resumedJustNow = {
    ...pausedStatus,
    is_scanning: true,
    state: 'running' as const,
    last_scan: {
      ...pausedStatus.last_scan, status: 'running', completed_at: null,
      started_at: new Date(Date.now() - 10_000).toISOString(),
    },
  }
  const progress = {
    queries_completed: 6, queries_total: 12, current_query: 'devops', current_page: 6,
    updated_at: null, percent: 55, pages_done_est: 66, pages_total_est: 120,
    current_query_pages_est: 10, estimated: true, eta_seconds: 4_320, eta_basis: 'checkpoint',
  }

  function renderCard(status: Record<string, unknown>, avgDurationSeconds: number | null = 9_000) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <ScannerStatusCard
          name="LinkedIn scan" queryKey="linkedin-status" triggerFn={vi.fn()} pauseFn={vi.fn()}
          resumeFn={vi.fn()} status={status as never} avgDurationSeconds={avgDurationSeconds}
        />
      </QueryClientProvider>,
    )
  }

  it('shows the backend percentage after a resume instead of dropping to ~1%', () => {
    renderCard({ ...resumedJustNow, progress })
    expect(screen.getByText('~55%')).toBeInTheDocument()
    expect(screen.queryByText('~1%')).toBeNull()
  })

  it('shows the ETA for the remaining work, not the full average duration', () => {
    renderCard({ ...resumedJustNow, progress })
    expect(screen.getByText('~1h 12m remaining (estimated)')).toBeInTheDocument()
    expect(screen.queryByText(/2h 30m/)).toBeNull()  // the old avg − elapsed estimate
  })

  it('says so when there is not enough data for an ETA', () => {
    renderCard({ ...resumedJustNow, progress: { ...progress, eta_seconds: null, eta_basis: null } })
    expect(screen.getByText('~55%')).toBeInTheDocument()
    expect(screen.getByText('Time remaining: not enough data yet')).toBeInTheDocument()
    expect(screen.queryByText(/remaining \(estimated\)/)).toBeNull()
  })

  it('shows an interrupted scan with its real progress and no counting ETA', () => {
    renderCard({
      ...pausedStatus, state: 'interrupted',
      last_scan: { ...pausedStatus.last_scan, status: 'interrupted' },
      progress: { ...progress, eta_seconds: null, eta_basis: null },
    })
    expect(screen.getByText(/Interrupted — resumes from checkpoint · ~55% done · Query 7 \/ 12/)).toBeInTheDocument()
    expect(screen.queryByText(/remaining/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled()
  })

  it('keeps the time-based estimate for a source without a work estimate (HelloWork)', () => {
    renderCard({
      ...resumedJustNow,
      last_scan: { ...resumedJustNow.last_scan, started_at: new Date(Date.now() - 60_000).toISOString() },
      progress: { slugs_completed: 3, updated_at: null },
    }, 300)
    expect(screen.getByText('~20%')).toBeInTheDocument()  // 60 s of a 300 s average
  })
})

describe('ScannerStatusCard — before any scan', () => {
  it('does not claim a completed scan when there has been none', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const neverScanned = { enabled: true, is_scanning: false, state: 'idle', progress: null, last_scan: null }
    render(<QueryClientProvider client={client}>{card(neverScanned, vi.fn())}</QueryClientProvider>)
    expect(screen.getByText('No scans yet.')).toBeTruthy()
    expect(screen.queryByText('Completed')).toBeNull()
    expect(screen.getByText('Idle')).toBeTruthy()
  })
})
