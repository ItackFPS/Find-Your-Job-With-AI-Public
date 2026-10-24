import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getRun, listRuns, getLinkedInStatus, listLinkedInScans, getHelloWorkStatus, listHelloWorkScans, type PipelineRun, type RunStats, type LinkedInScanRun, type LinkedInScanStats, type HelloWorkScanRun, type HelloWorkScanStats } from '../api/client'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import i18n from '../i18n'
import { Card, StatusBadge, SourceStatus, FunnelChart, DataTable, EmptyState, LoadingState, type DataTableColumn, type FunnelRow } from '../components'
import { findPipelineStage } from '../config/pipelineStages'
import { formatDuration, formatElapsedBetween, formatDateTime } from '../utils'

// Percentage drop from one funnel stage to the next, rendered as a small
// callout under the row it dropped INTO. Display-only — recomputes nothing,
// just formats numbers already present in RunStats.
function dropNote(from: number | undefined, to: number | undefined, label: string): string | undefined {
  if (typeof from !== 'number' || typeof to !== 'number' || from <= 0 || to > from) return undefined
  const dropPct = Math.round((1 - to / from) * 100)
  return dropPct > 0 ? i18n.t('pipeline.drop', { percent: dropPct, label }) : undefined
}

// Sourced from the same canonical stage definitions Dashboard.tsx uses
// (src/config/pipelineStages.ts) rather than reading s.hard_filter_input /
// s.requirements_gate_input / s.llm_eligibility_input directly here — this
// funnel and Dashboard's live funnel must never disagree about which
// RunStats field belongs to which stage.
const HARD_FILTER_STAGE = findPipelineStage('filter')!
const REQUIREMENTS_GATE_STAGE = findPipelineStage('requirements_gate')!
const LLM_ELIGIBILITY_STAGE = findPipelineStage('llm_eligibility')!

function pipelineFunnelRows(s: RunStats, t: TFunction): FunnelRow[] {
  const base = s.collected || 1
  // The hard filter (keyword/location/seniority/work-mode/etc., src/pipeline/filter.py),
  // the requirements gate (education/skills, src/requirements/gate.py), and the
  // optional AI eligibility check (implicit education/seniority, src/ai/eligibility.py)
  // are three separate, sequential gates — none re-filters another's output. All are
  // shown explicitly so a run never looks like the same number is "filtered twice".
  const hfInput = HARD_FILTER_STAGE.input(s)
  const hfPassed = HARD_FILTER_STAGE.done(s)
  const rgInput = REQUIREMENTS_GATE_STAGE.input(s)
  const rgPassed = REQUIREMENTS_GATE_STAGE.done(s)
  const elInput = LLM_ELIGIBILITY_STAGE.input(s)
  const elPassed = LLM_ELIGIBILITY_STAGE.done(s)
  return [
    { key: 'collected', label: t('pipeline.funnel.collected'), value: s.collected, max: base },
    { key: 'validated', label: t('pipeline.funnel.validated'), value: s.validated, max: base },
    { key: 'deduplicated', label: t('pipeline.funnel.deduplicated'), value: s.deduplicated, max: base },
    { key: 'hf-input', label: t('pipeline.funnel.input'), value: hfInput, max: base, groupLabel: t('pipeline.funnel.hardFilterGroup') },
    {
      key: 'hf-passed', label: t('pipeline.funnel.passed'), value: hfPassed, max: base,
      note: dropNote(hfInput, hfPassed, t('pipeline.funnel.hardFilterDrop')),
    },
    { key: 'rg-input', label: t('pipeline.funnel.input'), value: rgInput, max: base, groupLabel: t('pipeline.funnel.requirementsGroup') },
    {
      key: 'rg-passed', label: t('pipeline.funnel.passed'), value: rgPassed, max: base,
      note: dropNote(rgInput, rgPassed, t('pipeline.funnel.requirementsDrop')),
    },
    { key: 'el-input', label: t('pipeline.funnel.input'), value: elInput, max: base, groupLabel: t('pipeline.funnel.eligibilityGroup') },
    {
      key: 'el-passed', label: t('pipeline.funnel.passed'), value: elPassed, max: base,
      note: dropNote(elInput, elPassed, t('pipeline.funnel.eligibilityDrop')),
    },
    { key: 'scored', label: t('pipeline.funnel.scored'), value: s.scored, max: base },
    { key: 'semantic', label: t('pipeline.funnel.semantic'), value: s.semantically_matched, max: base },
    { key: 'personalized', label: t('pipeline.funnel.personalized'), value: s.personalized, max: base },
  ]
}

function FunnelRows({ s }: { s: RunStats }) {
  const { t } = useTranslation()
  return <FunnelChart rows={pipelineFunnelRows(s, t)} labelWidth="170px" />
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
  { key: 'name', header: t('pipeline.sourceTable.source'), render: r => <span style={{ fontFamily: 'monospace', fontSize: 'var(--font-size-sm)' }}>{r.name}</span> },
  { key: 'collected', header: t('pipeline.sourceTable.collected'), align: 'right', render: r => r.collected },
  { key: 'accepted', header: t('pipeline.sourceTable.accepted'), align: 'right', render: r => r.accepted },
  { key: 'errors', header: t('pipeline.sourceTable.errors'), align: 'right', render: r => <span style={{ color: r.errors > 0 ? 'var(--color-error)' : 'inherit' }}>{r.errors}</span> },
  { key: 'status', header: t('pipeline.sourceTable.status'), render: r => <SourceStatus status={r.status} reason={r.reason} /> },
]

function SourcesTable({ bySource }: { bySource: RunStats['by_source'] }) {
  const { t } = useTranslation()
  const entries = Object.entries(bySource)
  if (entries.length === 0) return null
  const rows: SourceRow[] = entries.map(([name, st]) => ({ name, ...st }))
  return (
    <div style={{ marginTop: 'var(--space-4)' }}>
      <div style={{ fontSize: 'var(--font-size-sm)', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: 'var(--space-2)' }}>
        {t('pipeline.sources')}
      </div>
      <DataTable columns={sourceColumns(t)} rows={rows} rowKey={r => r.name} />
    </div>
  )
}

function LiveRun({ runId }: { runId: string }) {
  const { t } = useTranslation()
  const { data: run } = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getRun(runId),
    refetchInterval: (q) => {
      const d = q.state.data
      if (!d || d.status === 'running') return 2000
      return false
    },
  })

  if (!run) return <LoadingState message={t('pipeline.live.connecting')} />

  const s = run.stats

  return (
    <Card
      style={{ marginBottom: 'var(--space-4)' }}
      title={run.status === 'running' ? t('pipeline.live.running') : run.status === 'completed' ? t('pipeline.live.complete') : t('pipeline.live.failed')}
      subtitle={<span style={{ fontFamily: 'monospace' }}>{runId}</span>}
      actions={<StatusBadge status={run.status} />}
    >
      {run.status === 'running' && s.current_stage && (
        <div style={{ marginBottom: '0.4rem', fontSize: 'var(--font-size-sm)', color: 'var(--color-warning)' }}>
          {t('pipeline.live.stage', { stage: s.current_stage === 'phase2' ? t('pipeline.live.phase2Stage') : s.current_stage })}
        </div>
      )}

      {run.status === 'running' && s.current_stage === 'phase2' && s.phase2 && (
        <div style={{ marginBottom: 'var(--space-4)', fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {t('pipeline.live.aiProgress', { done: s.phase2.completed ?? 0, total: s.phase2.total ?? 0 })}
          {typeof s.phase2.failed === 'number' && s.phase2.failed > 0 && (
            <span style={{ color: 'var(--color-error)' }}>{t('pipeline.live.aiFailed', { count: s.phase2.failed })}</span>
          )}
          {typeof s.phase2.percent === 'number' && t('pipeline.live.aiPercent', { percent: s.phase2.percent.toFixed(0) })}
          {typeof s.phase2.elapsed_seconds === 'number' && t('pipeline.live.aiElapsed', { seconds: s.phase2.elapsed_seconds.toFixed(0) })}
        </div>
      )}

      {s.use_ai && (
        <div style={{ marginBottom: 'var(--space-4)', fontSize: 'var(--font-size-xs)', color: 'var(--color-accent)' }}>
          {t('pipeline.live.aiEnabled')}
        </div>
      )}

      <div style={{ marginBottom: 'var(--space-2)' }}>
        <FunnelRows s={s} />
      </div>

      {s.duration_seconds > 0 && (
        <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)', marginTop: 'var(--space-3)' }}>
          {t('pipeline.duration', { duration: formatDuration(s.duration_seconds) })}
        </div>
      )}

      {run.status === 'failed' && run.error && (
        <div style={{ marginTop: 'var(--space-4)', padding: '0.75rem', background: 'var(--color-bg)', borderRadius: 'var(--radius-md)', border: '1px solid color-mix(in srgb, var(--color-error) 27%, transparent)', color: 'var(--color-error)', fontSize: '0.83rem', fontFamily: 'monospace' }}>
          {run.error}
        </div>
      )}

      {run.status === 'completed' && (
        <div style={{ marginTop: 'var(--space-4)', textAlign: 'right' }}>
          <Link to="/jobs" style={{ color: 'var(--color-accent)', fontSize: '0.88rem' }}>
            {t('pipeline.browseResults')}
          </Link>
        </div>
      )}

      {s.errors.length > 0 && (
        <div style={{ marginTop: 'var(--space-3)' }}>
          <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-error)', marginBottom: '0.25rem', fontWeight: 'var(--font-weight-semibold)' }}>{t('pipeline.collectorErrors', { count: s.errors.length })}</div>
          {s.errors.slice(0, 5).map((e, i) => (
            <div key={i} style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginBottom: '0.2rem' }}>• {e}</div>
          ))}
        </div>
      )}
    </Card>
  )
}

function RunDetailPanel({ run }: { run: PipelineRun }) {
  const { t } = useTranslation()
  const s = run.stats
  const started = formatDateTime(run.started_at)
  const ended = run.completed_at ? formatDateTime(run.completed_at) : null

  return (
    <Card elevated style={{ padding: '1rem 1.25rem', margin: '0.25rem 0 0.5rem' }}>
      {/* Timing */}
      <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)', marginBottom: 'var(--space-4)', display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap', alignItems: 'center' }}>
        <span>{t('pipeline.started', { date: started })}</span>
        {ended && <span>{t('pipeline.ended', { date: ended })}</span>}
        {s.duration_seconds > 0 && <span>{t('pipeline.duration', { duration: formatDuration(s.duration_seconds) })}</span>}
        {s.use_ai && <span style={{ color: 'var(--color-accent)' }}>{t('pipeline.aiBadge')}</span>}
      </div>

      {/* Funnel */}
      <FunnelRows s={s} />

      {/* Sources */}
      <SourcesTable bySource={s.by_source} />

      {/* Errors list */}
      {s.errors.length > 0 && (
        <div style={{ marginTop: 'var(--space-4)' }}>
          <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-error)', fontWeight: 'var(--font-weight-semibold)', marginBottom: '0.3rem' }}>
            {t('pipeline.collectorErrors', { count: s.errors.length })}
          </div>
          {s.errors.map((e, i) => (
            <div key={i} style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', marginBottom: '0.2rem' }}>• {e}</div>
          ))}
        </div>
      )}

      {/* Run-level error */}
      {run.status === 'failed' && run.error && (
        <div style={{ marginTop: 'var(--space-3)', padding: '0.6rem 0.75rem', background: 'var(--color-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid color-mix(in srgb, var(--color-error) 27%, transparent)', color: 'var(--color-error)', fontSize: '0.8rem', fontFamily: 'monospace' }}>
          {run.error}
        </div>
      )}

      {run.status === 'completed' && (
        <div style={{ marginTop: 'var(--space-3)', textAlign: 'right' }}>
          <Link to="/jobs" style={{ color: 'var(--color-accent)', fontSize: '0.83rem' }}>
            {t('pipeline.browseResults')}
          </Link>
        </div>
      )}
    </Card>
  )
}

function HistoryRow({ started, id, right, status, selected, onToggle, detail }: {
  started: string
  id: string
  right: React.ReactNode
  status: string
  selected: boolean
  onToggle: () => void
  detail: React.ReactNode
}) {
  return (
    <div>
      <div
        onClick={onToggle}
        style={{
          display: 'flex', alignItems: 'center', gap: 'var(--space-4)',
          padding: '0.65rem 0', borderTop: '1px solid var(--color-border)',
          flexWrap: 'wrap', cursor: 'pointer',
          background: selected ? 'var(--color-bg)' : 'transparent',
          borderRadius: selected ? 'var(--radius-sm)' : undefined,
          paddingLeft: selected ? '0.5rem' : undefined,
          transition: 'background 0.1s',
        }}
        onMouseEnter={e => { if (!selected) (e.currentTarget as HTMLDivElement).style.background = 'color-mix(in srgb, var(--color-bg) 13%, transparent)' }}
        onMouseLeave={e => { if (!selected) (e.currentTarget as HTMLDivElement).style.background = 'transparent' }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: '0.85rem', fontWeight: 'var(--font-weight-medium)' }}>{started}</div>
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', fontFamily: 'monospace', marginTop: '0.15rem' }}>{id}</div>
        </div>
        {right}
        <StatusBadge status={status} />
        <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-muted)', userSelect: 'none' }}>{selected ? '▲' : '▼'}</span>
      </div>
      {selected && detail}
    </div>
  )
}

function RunHistoryRow({ run, selected, onToggle }: { run: PipelineRun; selected: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  return (
    <HistoryRow
      started={formatDateTime(run.started_at)}
      id={run.id}
      status={run.status}
      selected={selected}
      onToggle={onToggle}
      right={
        <>
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{formatDuration(run.stats.duration_seconds)}</span>
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('pipeline.ranked', { count: run.stats.personalized })}</span>
        </>
      }
      detail={<RunDetailPanel run={run} />}
    />
  )
}

type QueryRow = LinkedInScanStats['queries'][number]

const queryColumns = (t: TFunction): DataTableColumn<QueryRow>[] => [
  { key: 'search_term', header: t('pipeline.queryTable.query'), render: r => <span style={{ fontFamily: 'monospace', fontSize: '0.76rem', display: 'inline-block', maxWidth: '180px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.search_term}>{r.search_term}</span> },
  { key: 'pages_fetched', header: t('pipeline.queryTable.pages'), align: 'right', render: r => r.pages_fetched },
  { key: 'jobs_raw', header: t('pipeline.queryTable.raw'), align: 'right', render: r => r.jobs_raw },
  { key: 'jobs_unique', header: t('pipeline.queryTable.unique'), align: 'right', render: r => r.jobs_unique },
  { key: 'jobs_filtered_out', header: t('pipeline.queryTable.filtered'), align: 'right', render: r => r.jobs_filtered_out },
  { key: 'jobs_already_known', header: t('pipeline.queryTable.known'), align: 'right', render: r => r.jobs_already_known },
  { key: 'jobs_staged', header: t('pipeline.queryTable.staged'), align: 'right', render: r => (
    <span style={{ fontWeight: r.jobs_staged > 0 ? 'var(--font-weight-semibold)' : 'var(--font-weight-normal)', color: r.jobs_staged > 0 ? 'var(--color-success)' : 'inherit' }}>{r.jobs_staged}</span>
  ) },
  { key: 'stopped_by', header: t('pipeline.queryTable.endedBy'), render: r => (
    <span style={{ color: r.stopped_by === 'error' ? 'var(--color-error)' : 'inherit' }}>{r.stopped_by === 'error' ? t('pipeline.queryTable.failedRetried') : (r.stopped_by ?? '–').replace('_', ' ')}</span>
  ) },
]

function ScanStatsPanel({ stats }: { stats: LinkedInScanStats }) {
  const { t, i18n } = useTranslation()
  return (
    <div style={{ marginTop: 'var(--space-4)' }}>
      <div style={{ display: 'flex', gap: '2rem', flexWrap: 'wrap', marginBottom: 'var(--space-4)' }}>
        {[
          { label: t('pipeline.scanStats.raw'), value: stats.total_raw },
          { label: t('pipeline.scanStats.unique'), value: stats.total_unique },
          { label: t('pipeline.scanStats.filteredOut'), value: stats.total_filtered_out },
          { label: t('pipeline.scanStats.known'), value: stats.total_already_known },
          { label: t('pipeline.scanStats.staged'), value: stats.total_staged },
          // A failed query (rate limited, blocked, network error) is retried by the next scan.
          { label: t('pipeline.scanStats.failedQueries'), value: stats.queries_failed ?? 0, alert: (stats.queries_failed ?? 0) > 0 },
        ].map(({ label, value, alert }) => (
          <div key={label}>
            <div style={{ fontSize: '0.73rem', color: 'var(--color-muted)', marginBottom: '0.1rem' }}>{label}</div>
            <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: '1rem', color: alert ? 'var(--color-error)' : 'inherit' }}>{value.toLocaleString(i18n.language)}</div>
          </div>
        ))}
      </div>
      {stats.queries.length > 0 && (
        <div>
          <div style={{ fontSize: '0.78rem', fontWeight: 'var(--font-weight-semibold)', color: 'var(--color-muted)', letterSpacing: '0.06em', marginBottom: 'var(--space-2)' }}>{t('pipeline.scanStats.queries')}</div>
          <DataTable columns={queryColumns(t)} rows={stats.queries} rowKey={r => r.search_term} />
        </div>
      )}
    </div>
  )
}

function ScanDetailPanel({ scan }: { scan: LinkedInScanRun }) {
  const { t } = useTranslation()
  const started = formatDateTime(scan.started_at)
  const ended = scan.completed_at ? formatDateTime(scan.completed_at) : null
  const dur = formatElapsedBetween(scan.started_at, scan.completed_at)

  return (
    <Card elevated style={{ padding: '1rem 1.25rem', margin: '0.25rem 0 0.5rem' }}>
      <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)', marginBottom: 'var(--space-3)', display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap' }}>
        <span>{t('pipeline.started', { date: started })}</span>
        {ended && <span>{t('pipeline.ended', { date: ended })}</span>}
        {scan.completed_at && <span>{t('pipeline.duration', { duration: dur })}</span>}
      </div>
      {scan.status === 'completed' && !scan.stats && (
        <div style={{ display: 'flex', gap: '2rem', flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', marginBottom: '0.15rem' }}>{t('pipeline.scanStats.jobsFound')}</div>
            <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: '1.1rem' }}>{scan.jobs_found}</div>
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', marginBottom: '0.15rem' }}>{t('pipeline.scanStats.newStaged')}</div>
            <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: '1.1rem', color: scan.jobs_new > 0 ? 'var(--color-success)' : 'inherit' }}>{scan.jobs_new}</div>
          </div>
        </div>
      )}
      {scan.stats && <ScanStatsPanel stats={scan.stats} />}
      {scan.status === 'interrupted' && scan.error && (
        <div style={{ marginTop: 'var(--space-3)', fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {scan.error}
        </div>
      )}
      {scan.status === 'failed' && scan.error && (
        <div style={{ marginTop: scan.stats ? 'var(--space-3)' : 0, padding: '0.6rem 0.75rem', background: 'var(--color-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid color-mix(in srgb, var(--color-error) 27%, transparent)', color: 'var(--color-error)', fontSize: '0.8rem', fontFamily: 'monospace' }}>
          {scan.error}
        </div>
      )}
    </Card>
  )
}

function ScanHistoryRow({ scan, selected, onToggle }: { scan: LinkedInScanRun; selected: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  return (
    <HistoryRow
      started={formatDateTime(scan.started_at)}
      id={scan.id}
      status={scan.status}
      selected={selected}
      onToggle={onToggle}
      right={
        scan.status === 'completed'
          ? <>
              <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{formatElapsedBetween(scan.started_at, scan.completed_at)}</span>
              <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('pipeline.foundNew', { found: scan.jobs_found, new: scan.jobs_new })}</span>
            </>
          : <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{formatElapsedBetween(scan.started_at, scan.completed_at)}</span>
      }
      detail={<ScanDetailPanel scan={scan} />}
    />
  )
}

function LinkedInScanHistory() {
  const { t } = useTranslation()
  const [selectedScanId, setSelectedScanId] = useState<string | null>(null)

  const { data: status } = useQuery({
    queryKey: ['linkedin-status'],
    queryFn: getLinkedInStatus,
  })

  const { data: scans, isLoading } = useQuery({
    queryKey: ['linkedin-scans'],
    queryFn: () => listLinkedInScans(20),
    enabled: status?.enabled === true,
    refetchInterval: (q) => {
      const d = q.state.data
      if (d?.some(s => s.status === 'running')) return 3000
      return false
    },
  })

  if (!status?.enabled) return null

  return (
    <>
      <h2 style={{ fontSize: '1rem', fontWeight: 'var(--font-weight-semibold)', margin: '1.5rem 0 0.5rem', color: 'var(--color-muted)' }}>
        {t('pipeline.linkedinHistory')}
      </h2>
      {isLoading ? (
        <LoadingState compact />
      ) : !scans || scans.length === 0 ? (
        <EmptyState message={t('pipeline.noScans')} />
      ) : (
        <Card style={{ padding: '0 1.25rem' }}>
          {scans.map(s => (
            <ScanHistoryRow
              key={s.id}
              scan={s}
              selected={selectedScanId === s.id}
              onToggle={() => setSelectedScanId(prev => prev === s.id ? null : s.id)}
            />
          ))}
        </Card>
      )}
    </>
  )
}

function HelloWorkScanStatsPanel({ stats }: { stats: HelloWorkScanStats }) {
  const { t } = useTranslation()
  return (
    <div style={{ marginTop: 'var(--space-4)', display: 'flex', gap: '2rem', flexWrap: 'wrap' }}>
      {[
        { label: t('pipeline.scanStats.metierPages'), value: stats.metier_slugs_queried },
        { label: t('pipeline.scanStats.collectorErrors'), value: stats.collector_errors },
        { label: t('pipeline.scanStats.collectionTime'), value: stats.collection_duration_seconds != null ? `${Math.round(stats.collection_duration_seconds)}s` : null },
      ].map(({ label, value }) => (
        <div key={label}>
          <div style={{ fontSize: '0.73rem', color: 'var(--color-muted)', marginBottom: '0.1rem' }}>{label}</div>
          <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: '1rem' }}>{value ?? '–'}</div>
        </div>
      ))}
      {stats.collector_error_samples && stats.collector_error_samples.length > 0 && (
        <ul style={{ flexBasis: '100%', margin: 0, paddingLeft: '1.1rem', fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {stats.collector_error_samples.map((msg, i) => <li key={i}>{msg}</li>)}
        </ul>
      )}
    </div>
  )
}

function HelloWorkScanDetailPanel({ scan }: { scan: HelloWorkScanRun }) {
  const { t } = useTranslation()
  const started = formatDateTime(scan.started_at)
  const ended = scan.completed_at ? formatDateTime(scan.completed_at) : null
  const dur = formatElapsedBetween(scan.started_at, scan.completed_at)

  return (
    <Card elevated style={{ padding: '1rem 1.25rem', margin: '0.25rem 0 0.5rem' }}>
      <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)', marginBottom: 'var(--space-3)', display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap' }}>
        <span>{t('pipeline.started', { date: started })}</span>
        {ended && <span>{t('pipeline.ended', { date: ended })}</span>}
        {scan.completed_at && <span>{t('pipeline.duration', { duration: dur })}</span>}
      </div>
      {scan.status === 'completed' && (
        <div style={{ display: 'flex', gap: '2rem', flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', marginBottom: '0.15rem' }}>{t('pipeline.scanStats.jobsFound')}</div>
            <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: '1.1rem' }}>{scan.jobs_found}</div>
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: 'var(--color-muted)', marginBottom: '0.15rem' }}>{t('pipeline.scanStats.newStaged')}</div>
            <div style={{ fontWeight: 'var(--font-weight-bold)', fontSize: '1.1rem', color: scan.jobs_new > 0 ? 'var(--color-success)' : 'inherit' }}>{scan.jobs_new}</div>
          </div>
        </div>
      )}
      {scan.stats && <HelloWorkScanStatsPanel stats={scan.stats} />}
      {scan.status === 'interrupted' && scan.error && (
        <div style={{ marginTop: 'var(--space-3)', fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>
          {scan.error}
        </div>
      )}
      {scan.status === 'failed' && scan.error && (
        <div style={{ marginTop: 'var(--space-3)', padding: '0.6rem 0.75rem', background: 'var(--color-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid color-mix(in srgb, var(--color-error) 27%, transparent)', color: 'var(--color-error)', fontSize: '0.8rem', fontFamily: 'monospace' }}>
          {scan.error}
        </div>
      )}
    </Card>
  )
}

function HelloWorkScanHistoryRow({ scan, selected, onToggle }: { scan: HelloWorkScanRun; selected: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  return (
    <HistoryRow
      started={formatDateTime(scan.started_at)}
      id={scan.id}
      status={scan.status}
      selected={selected}
      onToggle={onToggle}
      right={
        <>
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{formatElapsedBetween(scan.started_at, scan.completed_at)}</span>
          {scan.status === 'completed' && (
            <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-muted)' }}>{t('pipeline.foundNew', { found: scan.jobs_found, new: scan.jobs_new })}</span>
          )}
        </>
      }
      detail={<HelloWorkScanDetailPanel scan={scan} />}
    />
  )
}

function HelloWorkScanHistory() {
  const { t } = useTranslation()
  const [selectedScanId, setSelectedScanId] = useState<string | null>(null)

  const { data: status } = useQuery({
    queryKey: ['hellowork-status'],
    queryFn: getHelloWorkStatus,
  })

  const { data: scans, isLoading } = useQuery({
    queryKey: ['hellowork-scans'],
    queryFn: () => listHelloWorkScans(20),
    enabled: status?.enabled === true,
    refetchInterval: (q) => {
      const d = q.state.data
      if (d?.some(s => s.status === 'running')) return 5000
      return false
    },
  })

  if (!status?.enabled) return null

  return (
    <>
      <h2 style={{ fontSize: '1rem', fontWeight: 'var(--font-weight-semibold)', margin: '1.5rem 0 0.5rem', color: 'var(--color-muted)' }}>
        {t('pipeline.helloworkHistory')}
      </h2>
      {isLoading ? (
        <LoadingState compact />
      ) : !scans || scans.length === 0 ? (
        <EmptyState message={t('pipeline.noScans')} />
      ) : (
        <Card style={{ padding: '0 1.25rem' }}>
          {scans.map(s => (
            <HelloWorkScanHistoryRow
              key={s.id}
              scan={s}
              selected={selectedScanId === s.id}
              onToggle={() => setSelectedScanId(prev => prev === s.id ? null : s.id)}
            />
          ))}
        </Card>
      )}
    </>
  )
}

export default function Pipeline() {
  const { t } = useTranslation()
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)

  const { data: runs, isLoading: runsLoading } = useQuery({
    queryKey: ['runs'],
    queryFn: () => listRuns(20),
    refetchInterval: (q) => {
      const d = q.state.data
      // Poll while a run is active (either one we started or one already running)
      if (activeRunId || d?.some(r => r.status === 'running')) return 2000
      return false
    },
  })

  // Auto-attach to any already-running run on page load / navigation — runs
  // are now started from the Dashboard, so this page just watches/reports.
  useEffect(() => {
    if (!activeRunId && runs) {
      const running = runs.find(r => r.status === 'running')
      if (running) setActiveRunId(running.id)
    }
  }, [runs, activeRunId])

  return (
    <div>
      <h1 style={{ margin: '0 0 var(--space-6)', fontSize: 'var(--font-size-xl)', fontWeight: 'var(--font-weight-bold)' }}>{t('pipeline.title')}</h1>

      {activeRunId && <LiveRun key={activeRunId} runId={activeRunId} />}

      <h2 style={{ fontSize: '1rem', fontWeight: 'var(--font-weight-semibold)', margin: '1.5rem 0 0.5rem', color: 'var(--color-muted)' }}>
        {t('pipeline.history')}
      </h2>

      {runsLoading ? (
        <LoadingState compact />
      ) : !runs || runs.length === 0 ? (
        <EmptyState message={t('pipeline.noRuns')} />
      ) : (
        <Card style={{ padding: '0 1.25rem' }}>
          {runs.map(r => (
            <RunHistoryRow
              key={r.id}
              run={r}
              selected={selectedRunId === r.id}
              onToggle={() => setSelectedRunId(prev => prev === r.id ? null : r.id)}
            />
          ))}
        </Card>
      )}

      <LinkedInScanHistory />
      <HelloWorkScanHistory />
    </div>
  )
}
