import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getDashboard, startRun, getLinkedInStatus, triggerLinkedInScan, listLinkedInScans,
  pauseLinkedInScan, resumeLinkedInScan,
  getHelloWorkStatus, triggerHelloWorkScan, listHelloWorkScans,
  pauseHelloWorkScan, resumeHelloWorkScan,
  type RunStats, type SourceStats, type ScanState, type LinkedInScanProgress, type HelloWorkScanProgress,
} from '../api/client'
import { Link } from 'react-router-dom'
import { Trans, useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { Card, StatTile, StatusBadge, SourceStatus, Chip, ProgressBar, EmptyState, LoadingState, DataTable, Switch, GettingStarted, type DataTableColumn } from '../components'
import { PIPELINE_STAGES, type PipelineStageDef } from '../config/pipelineStages'
import { usePipelineEvents, stageLabel, type ConnectionState, type ActivityEntry } from '../hooks/usePipelineEvents'
import {
  formatDuration, formatDateTime, scannerControl, scannerProgressLabel, scannerWorkEstimate,
  startHoldEnded, type StartHold,
} from '../utils'

// ---------------------------------------------------------------------------
// LinkedIn / HelloWork scanner status cards — surfaces the same status
// already polled on the Pipeline page (getLinkedInStatus/getHelloWorkStatus),
// shown here too since they run independently of the main pipeline run.
//
// LinkedIn's percentage/ETA come from the backend's work-based estimate
// (issue #86): page-weighted from the checkpoint and remaining pages ×
// measured seconds per page, so a restart that resumes the checkpoint keeps
// them. HelloWork exposes no progress fraction, so its percentage/ETA stay
// an ESTIMATE from the average duration of its own past completed scans
// against elapsed time since `started_at`, capped below 100% while running.
// Both expose (issue #81) the persisted scan `state` (Pause/Resume control)
// and the durable checkpoint `progress` (query/page, or metier pages done).
// ---------------------------------------------------------------------------

interface ScannerStatus {
  enabled: boolean
  is_scanning: boolean
  last_scan: { id: string; status: string; started_at: string; completed_at: string | null; jobs_found: number; jobs_new: number } | null
  state?: ScanState
  progress?: LinkedInScanProgress | HelloWorkScanProgress | null
}

// Based on this scanner's own past completed scans — labeled "(estimated)"
// wherever it's shown since a scanner's runs can vary (e.g. rate-limited
// retries), but any real history is better than no estimate at all.
function averageDurationSeconds(scans: { status: string; started_at: string; completed_at: string | null }[] | undefined): number | null {
  if (!scans) return null
  const durations = scans
    .filter(s => s.status === 'completed' && s.completed_at)
    .map(s => (new Date(s.completed_at!).getTime() - new Date(s.started_at).getTime()) / 1000)
    .filter(d => d > 0)
  if (durations.length === 0) return null
  return durations.reduce((a, b) => a + b, 0) / durations.length
}

// Human-friendly duration for the ETA line (minutes/hours) — kept local
// since the shared `formatDuration` util intentionally stays in raw seconds
// for the short pipeline-run durations it's used for elsewhere.
function formatEta(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  const totalMinutes = Math.round(seconds / 60)
  if (totalMinutes < 60) return `${totalMinutes}m`
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`
}

export function ScannerStatusCard({ name, queryKey, triggerFn, pauseFn, resumeFn, status, avgDurationSeconds }: {
  name: string
  queryKey: string
  triggerFn: () => Promise<{ scan_id: string }>
  pauseFn: () => Promise<unknown>
  resumeFn: () => Promise<unknown>
  status: ScannerStatus | undefined
  avgDurationSeconds: number | null
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  // Set when Scan now / Resume is accepted; cleared only once the backend
  // confirms the start (see startHoldEnded in utils/scannerControl.ts).
  const [startHold, setStartHold] = useState<StartHold | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (startHold && status && startHoldEnded(startHold, status, now)) setStartHold(null)
  }, [startHold, status, now])

  const holdUntilStarted = () => {
    setStartHold({ fromScanId: status?.last_scan?.id ?? null, since: Date.now() })
    queryClient.invalidateQueries({ queryKey: [queryKey] })
  }

  const { mutate: triggerScan, isPending: scanPending } = useMutation({
    mutationFn: triggerFn,
    onSuccess: holdUntilStarted,
  })

  const { mutate: pauseScan, isPending: pausePending } = useMutation({
    mutationFn: pauseFn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [queryKey] }),
  })

  const { mutate: resumeScan, isPending: resumePending } = useMutation({
    mutationFn: resumeFn,
    onSuccess: holdUntilStarted,
  })

  const isActivelyScanning = scanPending || resumePending || (status?.is_scanning ?? false) || startHold !== null

  useEffect(() => {
    if (!isActivelyScanning) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [isActivelyScanning])

  if (!status || !status.enabled) return null
  const scan = status.last_scan
  // An accepted Scan now/Resume reads as "Starting…" until the backend confirms it.
  const control = scannerControl(
    status,
    scanPending || pausePending || resumePending,
    startHold !== null && !status.is_scanning,
  )
  const progressLabel = scannerProgressLabel(status.progress)
  const onControl = () => {
    if (control.action === 'pause') pauseScan()
    else if (control.action === 'resume') resumeScan()
    else if (control.action === 'scan') triggerScan()
  }
  const controlEnabled = !control.disabled && control.action !== 'none'

  // Only a scan the backend reports running has a meaningful started_at:
  // while "Starting…" is shown, last_scan is still the previous run, so the
  // bar stays indeterminate until the new scan is confirmed.
  let estimate: { pct: number; etaLabel: string } | null = null
  const work = scannerWorkEstimate(status.progress)
  if (status.is_scanning && work) {
    // Backend work-based estimate (LinkedIn): continues from the checkpoint
    // after a restart; no ETA is shown rather than a guess (issue #86).
    const etaLabel = work.etaSeconds === null
      ? control.state === 'pausing' ? t('dashboard.scanner.pausingNextPage') : t('dashboard.scanner.etaUnknown')
      : work.etaSeconds > 5 ? t('dashboard.scanner.etaRemaining', { eta: formatEta(work.etaSeconds) }) : t('dashboard.scanner.finishing')
    estimate = { pct: work.percent, etaLabel }
  } else if (status.is_scanning && scan?.started_at && avgDurationSeconds) {
    const elapsed = (now - new Date(scan.started_at).getTime()) / 1000
    const pct = Math.max(1, Math.min(97, Math.round((elapsed / avgDurationSeconds) * 100)))
    const remaining = avgDurationSeconds - elapsed
    const etaLabel = remaining > 5
      ? t('dashboard.scanner.etaRemaining', { eta: formatEta(remaining) })
      : elapsed > avgDurationSeconds * 1.3
        ? t('dashboard.scanner.slow')
        : t('dashboard.scanner.finishing')
    estimate = { pct, etaLabel }
  }

  return (
    <Card>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 'var(--space-3)' }}>
        <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>{name}</div>
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
          <StatusBadge status={control.state === 'idle' ? (scan?.status ?? 'idle') : control.state} />
          <button
            onClick={onControl}
            disabled={!controlEnabled}
            style={{
              background: 'transparent',
              border: `1px solid ${controlEnabled ? 'var(--color-accent)' : 'var(--color-border)'}`,
              borderRadius: 'var(--radius-sm)',
              padding: '0.2rem 0.65rem',
              fontSize: 'var(--font-size-xs)',
              cursor: controlEnabled ? 'pointer' : 'not-allowed',
              color: controlEnabled ? 'var(--color-accent)' : 'var(--color-muted)',
            }}
          >
            {control.label}
          </button>
        </div>
      </div>
      {isActivelyScanning ? (
        <div>
          {estimate ? (
            <>
              <ProgressBar value={estimate.pct} tone="accent" label={t('dashboard.scanner.searching')} rightLabel={`~${estimate.pct}%`} />
              <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginTop: '0.3rem' }}>{estimate.etaLabel}</div>
            </>
          ) : (
            <ProgressBar indeterminate tone="accent" label={t('dashboard.scanner.searching')} />
          )}
          {progressLabel && (
            <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginTop: '0.3rem' }}>{progressLabel}</div>
          )}
        </div>
      ) : control.state === 'paused' || control.state === 'interrupted' ? (
        <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {control.state === 'paused' ? t('dashboard.scanner.paused') : t('dashboard.scanner.interrupted')}
          {work ? t('dashboard.scanner.percentDone', { percent: work.percent }) : ''}
          {progressLabel ? ` · ${progressLabel}` : ''}
        </div>
      ) : scan ? (
        <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {scan.status === 'completed'
            ? t('dashboard.scanner.foundNew', { found: scan.jobs_found, new: scan.jobs_new })
            : t('dashboard.scanner.lastAttempt', { date: formatDateTime(scan.started_at) })}
        </div>
      ) : (
        <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('dashboard.scanner.noScans')}</div>
      )}
    </Card>
  )
}

function ScannerStatusRow() {
  const { t } = useTranslation()
  const { data: liStatus } = useQuery({ queryKey: ['linkedin-status'], queryFn: getLinkedInStatus, refetchInterval: 5000 })
  const { data: hwStatus } = useQuery({ queryKey: ['hellowork-status'], queryFn: getHelloWorkStatus, refetchInterval: 5000 })
  const { data: liScans } = useQuery({ queryKey: ['linkedin-scans'], queryFn: () => listLinkedInScans(20), enabled: liStatus?.enabled === true })
  const { data: hwScans } = useQuery({ queryKey: ['hellowork-scans'], queryFn: () => listHelloWorkScans(20), enabled: hwStatus?.enabled === true })

  if ((liStatus && !liStatus.enabled) && (hwStatus && !hwStatus.enabled)) return null
  if (!liStatus && !hwStatus) return null

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--space-4)', marginBottom: 'var(--space-5)' }}>
      <ScannerStatusCard name={t('dashboard.scanner.linkedin')} queryKey="linkedin-status" triggerFn={triggerLinkedInScan} pauseFn={pauseLinkedInScan} resumeFn={resumeLinkedInScan} status={liStatus} avgDurationSeconds={averageDurationSeconds(liScans)} />
      <ScannerStatusCard name={t('dashboard.scanner.hellowork')} queryKey="hellowork-status" triggerFn={triggerHelloWorkScan} pauseFn={pauseHelloWorkScan} resumeFn={resumeHelloWorkScan} status={hwStatus} avgDurationSeconds={averageDurationSeconds(hwScans)} />
    </div>
  )
}

interface SourceRow {
  name: string
  collected: number
  accepted: number
  errors: number
  status?: string | null
  reason?: string | null
}

const sourceColumns = (t: TFunction): DataTableColumn<SourceRow>[] => [
  { key: 'name', header: t('dashboard.sourceTable.source'), render: r => <span style={{ fontWeight: 'var(--font-weight-medium)' }}>{r.name}</span> },
  { key: 'collected', header: t('dashboard.sourceTable.collected'), align: 'right', render: r => <span style={{ color: 'var(--color-muted)' }}>{r.collected}</span> },
  { key: 'accepted', header: t('dashboard.sourceTable.accepted'), align: 'right', render: r => <span style={{ color: 'var(--color-success)' }}>{r.accepted}</span> },
  { key: 'rejected', header: t('dashboard.sourceTable.rejected'), align: 'right', render: r => <span style={{ color: 'var(--color-muted)' }}>{r.collected - r.accepted}</span> },
  { key: 'errors', header: t('dashboard.sourceTable.errors'), align: 'right', render: r => <span style={{ color: r.errors > 0 ? 'var(--color-error)' : 'var(--color-muted)' }}>{r.errors}</span> },
  { key: 'status', header: t('dashboard.sourceTable.status'), render: r => <SourceStatus status={r.status} reason={r.reason} /> },
]

function BySourceTable({ bySource }: { bySource: Record<string, SourceStats> }) {
  const { t } = useTranslation()
  const rows: SourceRow[] = Object.entries(bySource)
    .map(([name, s]) => ({ name, ...s }))
    .sort((a, b) => b.collected - a.collected)
  if (rows.length === 0) return null
  return <DataTable columns={sourceColumns(t)} rows={rows} rowKey={r => r.name} />
}

function CountryList({ byCountry }: { byCountry: Record<string, number> }) {
  const { t } = useTranslation()
  const sorted = Object.entries(byCountry).sort((a, b) => b[1] - a[1]).slice(0, 10)
  if (sorted.length === 0) return <span style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-sm)' }}>{t('common.noData')}</span>
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)' }}>
      {sorted.map(([c, n]) => (
        <Chip key={c}>{c} <strong>{n}</strong></Chip>
      ))}
    </div>
  )
}

function SectionLabel({ children, color }: { children: React.ReactNode; color?: string }) {
  return (
    <h3 style={{ marginBottom: 'var(--space-3)', fontSize: 'var(--font-size-base)', fontWeight: 'var(--font-weight-semibold)', color: color ?? 'var(--color-muted)' }}>
      {children}
    </h3>
  )
}

// ---------------------------------------------------------------------------
// CurrentRunCard — the single, permanent "what's the pipeline doing" card.
//
// Sourced from whichever data is freshest: the live SSE event while one is
// being tracked, falling back to the last completed run from GET /dashboard
// so the same rich view (stage funnel, by-source/by-country breakdown) stays
// on screen at all times instead of collapsing to a plainer summary between
// runs or before the SSE connection delivers its first event.
// ---------------------------------------------------------------------------

interface CurrentRun {
  status: string
  stage: string
  stats: RunStats
  error: string | null
  startedAt: string | null
}

function CurrentRunCard({ run, activity, isLive }: { run: CurrentRun; activity: ActivityEntry[]; isLive: boolean }) {
  const { t } = useTranslation()
  const s = run.stats

  return (
    <Card
      style={{ marginBottom: 'var(--space-5)', borderColor: run.status === 'running' ? 'var(--color-accent)' : 'var(--color-border)' }}
      title={isLive ? t('dashboard.liveRun') : t('dashboard.latestRun')}
      subtitle={!isLive && run.startedAt ? `${formatDateTime(run.startedAt)} · ${formatDuration(s.duration_seconds)}` : undefined}
      actions={
        <>
          {s.model_name && <Chip>{s.model_name}</Chip>}
          <StatusBadge status={run.status} />
        </>
      }
    >
      {run.status === 'running' && (
        <div style={{ marginBottom: 'var(--space-4)' }}>
          <div style={{ fontSize: 'var(--font-size-base)', fontWeight: 'var(--font-weight-semibold)', marginBottom: '0.4rem' }}>
            {stageLabel(run.stage)}
          </div>
          <StageProgress stage={run.stage} stats={s} />
        </div>
      )}

      {run.status === 'failed' && run.error && (
        <div style={{ marginBottom: 'var(--space-4)', fontSize: 'var(--font-size-sm)', color: 'var(--color-error)' }}>
          {run.error}
        </div>
      )}

      <div style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap', marginBottom: 'var(--space-5)' }}>
        <StatTile label={t('dashboard.tiles.collected')} value={s.collected} />
        <StatTile label={t('dashboard.tiles.validated')} value={s.validated} />
        <StatTile label={t('dashboard.tiles.deduplicated')} value={s.deduplicated} />
        <StatTile label={t('dashboard.tiles.filtered')} value={s.filtered} />
        <StatTile label={t('dashboard.tiles.scored')} value={s.scored} />
        <StatTile label={t('dashboard.tiles.matched')} value={s.personalized} tone="success" />
        {s.errors.length > 0 && <StatTile label={t('dashboard.tiles.errors')} value={s.errors.length} tone="error" />}
      </div>

      <SectionLabel>{t('dashboard.sections.stages')}</SectionLabel>
      <StageFunnelLive stats={s} currentStage={run.stage} runStatus={run.status} />

      {Object.keys(s.by_source).length > 0 && (
        <div style={{ marginTop: 'var(--space-5)' }}>
          <SectionLabel>{run.stage === 'collect' ? t('dashboard.sections.bySourceLive') : t('dashboard.sections.bySource')}</SectionLabel>
          <BySourceTable bySource={s.by_source} />
        </div>
      )}

      {Object.keys(s.by_country).length > 0 && (
        <div style={{ marginTop: 'var(--space-5)' }}>
          <SectionLabel>{t('dashboard.sections.byCountry')}</SectionLabel>
          <CountryList byCountry={s.by_country} />
        </div>
      )}

      {activity.length > 0 && (
        <div style={{ marginTop: 'var(--space-5)' }}>
          <SectionLabel>{t('dashboard.sections.activity')}</SectionLabel>
          <ActivityLog entries={activity} />
        </div>
      )}

      {s.errors.length > 0 && (
        <div style={{ marginTop: 'var(--space-5)' }}>
          <SectionLabel color="var(--color-error)">{t('dashboard.sections.errors')}</SectionLabel>
          <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
            {s.errors.slice(0, 5).map((e, i) => <div key={i} style={{ marginBottom: '0.25rem' }}>• {e}</div>)}
            {s.errors.length > 5 && <div>{t('dashboard.moreErrors', { count: s.errors.length - 5 })}</div>}
          </div>
        </div>
      )}
    </Card>
  )
}

function ConnectionBadge({ connection }: { connection: ConnectionState }) {
  const { t } = useTranslation()
  const meta: Record<ConnectionState, { label: string; color: string }> = {
    connecting: { label: t('dashboard.connection.connecting'), color: 'var(--color-muted)' },
    live: { label: t('dashboard.connection.live'), color: 'var(--color-success)' },
    reconnecting: { label: t('dashboard.connection.reconnecting'), color: 'var(--color-warning)' },
    offline: { label: t('dashboard.connection.offline'), color: 'var(--color-error)' },
  }
  const m = meta[connection]
  return <span style={{ fontSize: 'var(--font-size-xs)', fontWeight: 'var(--font-weight-semibold)', color: m.color }}>{m.label}</span>
}

// Phase 2 keeps its own richer bar (percent + elapsed_seconds already
// computed server-side in stats.phase2) — this is the "currently running"
// header bar shown above the funnel; the funnel below additionally shows
// Phase 2 as one row among all stages via the generic stage_progress path.
function StageProgress({ stage, stats }: { stage: string; stats: RunStats }) {
  const p2 = stats.phase2
  if (stage === 'phase2' && p2 && typeof p2.total === 'number' && p2.total > 0) {
    const completed = p2.completed ?? 0
    const pct = Math.min(100, Math.round((completed / p2.total) * 100))
    return <ProgressBar value={pct} label={`${completed.toLocaleString()} / ${p2.total.toLocaleString()}`} rightLabel={`${pct}%`} />
  }
  const live = stats.stage_progress
  if (live && live.stage === stage && typeof live.total === 'number' && live.total > 0) {
    const processed = live.processed ?? 0
    const pct = Math.min(100, Math.round((processed / live.total) * 100))
    return <ProgressBar value={pct} label={`${processed.toLocaleString()} / ${live.total.toLocaleString()}`} rightLabel={`${pct}%`} />
  }
  // No reliable denominator yet for this stage — show an indeterminate
  // state rather than inventing a percentage.
  return <ProgressBar indeterminate />
}

// ---------------------------------------------------------------------------
// Full-pipeline stage funnel — one row per stage, in actual execution order.
// Distinguishes Hard Filter from Requirements Gate (separate stages, separate
// input/output counts) rather than collapsing them into one "filtering" row.
// ---------------------------------------------------------------------------

function StageFunnelRow({ def, status, stats }: { def: PipelineStageDef; status: 'done' | 'running' | 'pending'; stats: RunStats }) {
  const { t } = useTranslation()
  const input = def.input(stats)
  const done = def.done(stats)
  const live = stats.stage_progress
  const liveMatch = status === 'running' && live && live.stage === def.key && typeof live.total === 'number' && live.total > 0

  let rightLabel: string
  if (liveMatch) {
    rightLabel = t('dashboard.funnel.liveCount', { processed: live!.processed ?? 0, total: live!.total!, unit: def.unit })
  } else if (status === 'done') {
    rightLabel = typeof done === 'number'
      ? (typeof input === 'number' ? `${done.toLocaleString()} / ${input.toLocaleString()}` : done.toLocaleString())
      : '–'
  } else if (status === 'running') {
    rightLabel = t('dashboard.funnel.working')
  } else {
    rightLabel = '–'
  }

  return (
    <div style={{ marginBottom: '0.6rem', opacity: status === 'pending' ? 0.45 : 1 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--font-size-sm)', marginBottom: '0.25rem' }}>
        <span style={{ fontWeight: status === 'running' ? 'var(--font-weight-bold)' : 'var(--font-weight-medium)' }}>{def.label}</span>
        <span style={{ color: 'var(--color-muted)' }}>{rightLabel}</span>
      </div>
      {liveMatch ? (
        <ProgressBar height="6px" value={Math.min(100, Math.round((100 * (live!.processed ?? 0)) / live!.total!))} />
      ) : status === 'done' ? (
        <div style={{ background: 'var(--color-success)', borderRadius: 'var(--radius-sm)', height: '6px', opacity: 0.6 }} />
      ) : status === 'running' ? (
        <ProgressBar height="6px" indeterminate />
      ) : (
        <div style={{ background: 'var(--color-bg)', borderRadius: 'var(--radius-sm)', height: '6px' }} />
      )}
    </div>
  )
}

function StageFunnelLive({ stats, currentStage, runStatus }: { stats: RunStats; currentStage: string; runStatus: string }) {
  // A terminal event (completed/failed) carries stage="" (the run is no
  // longer "at" any stage) — fall back to the last stage_progress update so
  // a failure still highlights where it happened, and treat "completed" as
  // every stage done rather than leaving the funnel looking unfinished.
  const effectiveStage = currentStage || stats.stage_progress?.stage || ''
  const currentIndex = PIPELINE_STAGES.findIndex(d => d.key === effectiveStage)
  const allDone = runStatus === 'completed'
  return (
    <div>
      {PIPELINE_STAGES.map((def, i) => {
        const status: 'done' | 'running' | 'pending' = allDone
          ? 'done'
          : currentIndex < 0 ? 'pending' : i < currentIndex ? 'done' : i === currentIndex ? 'running' : 'pending'
        return <StageFunnelRow key={def.key} def={def} status={status} stats={stats} />
      })}
    </div>
  )
}

function ActivityLog({ entries }: { entries: ActivityEntry[] }) {
  if (entries.length === 0) return null
  return (
    <div style={{ maxHeight: '160px', overflowY: 'auto', fontSize: 'var(--font-size-xs)', fontFamily: 'monospace' }}>
      {entries.slice().reverse().map(e => (
        <div key={e.id} style={{ padding: '0.15rem 0', color: 'var(--color-muted)' }}>
          <span style={{ opacity: 0.7 }}>{e.time}</span>{'  '}
          <span style={{ color: 'var(--color-text)' }}>{e.text}</span>
        </div>
      ))}
    </div>
  )
}

export default function Dashboard() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: ['dashboard'],
    queryFn: getDashboard,
    refetchInterval: 5000,
  })
  const { connection, liveEvent, activity } = usePipelineEvents()

  const [useAi, setUseAi] = useState(false)

  // Runs the pipeline directly from the Dashboard — the SSE connection above
  // (usePipelineEvents) picks up its progress automatically, so there's no
  // need to navigate to the Pipeline page to watch it run.
  const { mutate: triggerRun, isPending: runPending, error: runError } = useMutation({
    mutationFn: () => startRun(useAi),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
  })

  if (isLoading) return <LoadingState />
  if (error) {
    return (
      <div style={{ color: 'var(--color-error)', padding: '2rem', lineHeight: 1.6 }}>
        <Trans
          i18nKey="dashboard.backendError"
          components={{ app: <strong />, cmd: <code>FindYourJob.cmd</code>, log: <code>{'logs\\launcher\\launcher.log'}</code> }}
        />
      </div>
    )
  }

  const d = data!

  // Prefer the live SSE event when one is being tracked; otherwise fall back
  // to the last completed run from the polled dashboard payload, so the same
  // rich "current run" view stays on screen at all times (see CurrentRunCard).
  const currentRun: CurrentRun | null = liveEvent
    ? { status: liveEvent.status, stage: liveEvent.stage, stats: liveEvent.stats, error: liveEvent.error, startedAt: null }
    : d.latest_run
      ? { status: d.latest_run.status, stage: d.latest_run.stats.current_stage || '', stats: d.latest_run.stats, error: d.latest_run.error, startedAt: d.latest_run.started_at }
      : null
  const matchedCount = currentRun?.stats.personalized ?? 0
  const isAlreadyRunning = currentRun?.status === 'running' || d.is_running

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-6)', flexWrap: 'wrap', gap: 'var(--space-3)' }}>
        <h1 style={{ margin: 0, fontSize: 'var(--font-size-xl)', fontWeight: 'var(--font-weight-bold)' }}>{t('dashboard.title')}</h1>
        <div style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center' }}>
          <ConnectionBadge connection={connection} />
          <span style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-sm)' }}>
            {t('dashboard.jobsInDb', { count: d.total_jobs_in_db })}
          </span>
          {!liveEvent && d.is_running && (
            <span style={{ color: 'var(--color-warning)', fontSize: 'var(--font-size-sm)', fontWeight: 'var(--font-weight-semibold)' }}>
              {t('dashboard.pipelineRunning')}
            </span>
          )}
          {runError && (
            <span style={{ color: 'var(--color-error)', fontSize: 'var(--font-size-sm)' }}>
              {(runError as Error).message}
            </span>
          )}
          <Switch
            checked={useAi}
            onChange={setUseAi}
            disabled={runPending || isAlreadyRunning}
            label={t('dashboard.ai.label')}
            description={t('dashboard.ai.description')}
          />
          <button
            onClick={() => triggerRun()}
            disabled={runPending || isAlreadyRunning}
            style={{
              background: (runPending || isAlreadyRunning) ? 'var(--color-border)' : 'var(--color-accent)',
              color: 'var(--color-on-accent)',
              border: 'none',
              borderRadius: 'var(--radius-md)',
              padding: '0.45rem 1rem',
              fontSize: 'var(--font-size-base)',
              fontWeight: 'var(--font-weight-semibold)',
              cursor: (runPending || isAlreadyRunning) ? 'not-allowed' : 'pointer',
            }}
          >
            {runPending ? t('dashboard.run.starting') : isAlreadyRunning ? t('dashboard.run.running') : t('dashboard.run.start')}
          </button>
        </div>
      </div>

      {!currentRun && d.total_jobs_in_db === 0 && <GettingStarted />}

      <ScannerStatusRow />

      {currentRun ? (
        <CurrentRunCard run={currentRun} activity={activity} isLive={liveEvent !== null} />
      ) : (
        <Card>
          <EmptyState
            message={<Trans i18nKey="dashboard.noRuns" components={{ run: <button onClick={() => triggerRun()} disabled={runPending} style={{ background: 'none', border: 'none', padding: 0, color: 'var(--color-accent)', cursor: runPending ? 'not-allowed' : 'pointer', font: 'inherit', textDecoration: 'underline' }} /> }} />}
          />
        </Card>
      )}

      {matchedCount > 0 && (
        <div style={{ marginTop: 'var(--space-4)', textAlign: 'center' }}>
          <Link to="/jobs" style={{ color: 'var(--color-accent)', fontSize: 'var(--font-size-base)' }}>
            {t('dashboard.browseMatched', { count: matchedCount })}
          </Link>
        </div>
      )}
    </div>
  )
}
