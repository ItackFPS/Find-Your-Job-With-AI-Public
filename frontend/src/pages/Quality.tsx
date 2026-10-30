import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  cancelQualityJob, getLabExperiments, getQualityHealth, getQualityJob, listQualityJobs, listRuns, startQualityJob,
  type LabExperiment, type PipelineRun, type QualityDatasetSummary, type QualityJob, type QualityJobKind,
  type QualitySummary,
} from '../api/client'
import {
  Button, Card, Chip, DataTable, EmptyState, ErrorState, LoadingState, StatusBadge, Stepper, Switch, useToast,
  type ChipTone, type DataTableColumn,
} from '../components'
import { formatDateTime, promotionCommand } from '../utils'
import Lab from './Lab'
import CalibrationCard from './Calibration'

// Plain-language quality page (plan F9.1, §C10). Every sentence is built here from the codes and
// numbers the API returns; nothing from the lab is rendered as HTML or as an acting link (ADR 0011:
// lab output is untrusted data). Promotion stays a human CLI action (owner decision I3).

const STATUS_TONE: Record<string, ChipTone> = {
  pass: 'success', measured: 'info', changed: 'warning', regression: 'error', baseline_mismatch: 'error',
  safety_violation: 'error',
}
const JOB_TONE: Record<string, ChipTone> = { running: 'info', completed: 'success', failed: 'error', cancelled: 'default' }
const PHASES = ['starting', 'running', 'summarising'] as const
const KNOWN_STATUS = new Set(Object.keys(STATUS_TONE))
const KNOWN_ERRORS = new Set(['busy', 'tool_failed', 'no_report', 'unreadable_report', 'timeout', 'unexpected', 'interrupted'])
const KNOWN_FLAGS = new Set([
  'synthetic_benchmark', 'few_suitable_labels', 'labels_not_independent', 'review_insufficient_evidence',
  'baseline_missing', 'baseline_mismatch',
])

function errorCodeOf(err: unknown): string | null {
  const detail = (err as { response?: { status?: number; data?: { detail?: { code?: string } } } })?.response
  if (detail?.status === 409) return 'busy'
  return detail?.data?.detail?.code ?? null
}

// Presentational: one dataset's result as plain sentences.
export function DatasetSentences({ d }: { d: QualityDatasetSummary }) {
  const { t } = useTranslation()
  return (
    <ul style={{ margin: 'var(--space-2) 0 0', paddingLeft: '1.1rem', display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
      <li style={{ color: d.unsafe_rejections ? 'var(--color-error)' : undefined }}>
        {d.unsafe_rejections === 0 ? t('quality.sentences.safe') : t('quality.sentences.unsafe', { count: d.unsafe_rejections })}
      </li>
      {d.precision != null && (
        <li>{t('quality.sentences.precision', { precision: d.precision, falsePositives: d.false_positives ?? 0 })}</li>
      )}
      {d.recall != null && <li>{t('quality.sentences.recall', { recall: d.recall })}</li>}
      {!!d.rejections_without_reason && <li>{t('quality.sentences.noReason', { count: d.rejections_without_reason })}</li>}
      {d.flips && d.flips.fixed + d.flips.broken > 0 && (
        <li>{t('quality.sentences.flips', { fixed: d.flips.fixed, broken: d.flips.broken })}</li>
      )}
      <li style={{ color: 'var(--color-muted)' }}>{t('quality.sentences.rankingNotMeasured')}</li>
      {d.flags.filter(f => KNOWN_FLAGS.has(f)).map(f => (
        <li key={f} style={{ color: 'var(--color-muted)' }}>{t(`quality.flags.${f}`, { count: d.suitable ?? 0 })}</li>
      ))}
    </ul>
  )
}

// Presentational: a self-check or evaluation summary.
export function SummaryView({ s }: { s: QualitySummary }) {
  const { t } = useTranslation()
  const status = s.status && KNOWN_STATUS.has(s.status) ? s.status : null
  const fb = s.feedback
  return (
    <div style={{ fontSize: 'var(--font-size-sm)' }}>
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
        {status && <Chip tone={STATUS_TONE[status]}>{t(`quality.status.${status}.label`)}</Chip>}
        {status && <span>{t(`quality.status.${status}.text`)}</span>}
      </div>
      {s.created_at && (
        <div style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-xs)', marginTop: 'var(--space-1)' }}>
          {t('quality.checkedAt', { date: formatDateTime(s.created_at) })}
        </div>
      )}
      {s.datasets.map((d, i) => (
        <div key={`${d.id}-${i}`} style={{ marginTop: 'var(--space-2)' }}>
          {s.datasets.length > 1 && <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>{d.id}</div>}
          <DatasetSentences d={d} />
        </div>
      ))}
      {fb.status === 'measured' && (
        <p style={{ margin: 'var(--space-2) 0 0' }}>
          {t('quality.feedback.measured', {
            answered: fb.answered ?? 0, decided: fb.decided ?? 0, coverage: fb.coverage ?? 0, contradictions: fb.contradictions,
          })}
        </p>
      )}
      {fb.status === 'unavailable' && <p style={{ margin: 'var(--space-2) 0 0', color: 'var(--color-muted)' }}>{t('quality.feedback.unavailable')}</p>}
    </div>
  )
}

function shortResult(job: QualityJob, t: (k: string, o?: Record<string, unknown>) => string): string {
  if (job.status === 'failed') return t(`quality.errors.${job.error_code && KNOWN_ERRORS.has(job.error_code) ? job.error_code : 'unexpected'}`)
  if (job.status !== 'completed' || !job.summary) return ''
  if (!('datasets' in job.summary)) return t('quality.personal.short', { labels: job.summary.dataset.labels ?? 0 })
  const d = job.summary.datasets[0]
  if (!d) return ''
  return t('quality.history.result', { unsafe: d.unsafe_rejections, precision: d.precision ?? 0, recall: d.recall ?? 0 })
}

// Presentational: progress of the running job, with a cancel action.
export function JobProgress({ job, onCancel, cancelling }: { job: QualityJob; onCancel: () => void; cancelling: boolean }) {
  const { t } = useTranslation()
  const current = Math.max(0, PHASES.indexOf((job.phase ?? 'starting') as typeof PHASES[number]))
  return (
    <div role="status" aria-live="polite" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
      <div>{t(`quality.actions.running.${job.kind}`)}</div>
      <Stepper label={t('quality.actions.progressLabel')} current={current}
        steps={PHASES.map(p => ({ key: p, label: t(`quality.phases.${p}`) }))} />
      <div>
        <Button variant="danger" onClick={onCancel} disabled={cancelling}>
          {cancelling ? t('quality.actions.cancelling') : t('quality.actions.cancel')}
        </Button>
      </div>
    </div>
  )
}

function useCopy() {
  const { t } = useTranslation()
  const toast = useToast()
  return async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.show({ message: t('quality.promotion.copied'), tone: 'success' })
    } catch {
      toast.show({ message: t('quality.promotion.copyFailed'), tone: 'warning' })
    }
  }
}

// Presentational: the CLI-only promotion step (owner decision I3), with a copy button.
export function PromotionSection({ experiments }: { experiments: LabExperiment[] }) {
  const { t } = useTranslation()
  const copy = useCopy()
  const placeholders = {
    experiment: t('quality.promotion.placeholders.experiment'), dataset: t('quality.promotion.placeholders.dataset'),
    name: t('quality.promotion.placeholders.name'), reason: t('quality.promotion.placeholders.reason'),
  }
  const waiting = experiments.filter(e => e.eligible_for_approval && e.candidate_status !== 'approved')
  const commandBlock = (cmd: string) => (
    <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap', marginTop: 'var(--space-2)' }}>
      <code style={{
        flex: '1 1 20rem', padding: 'var(--space-2) var(--space-3)', borderRadius: 'var(--radius-md)',
        background: 'var(--color-bg)', border: '1px solid var(--color-border)', overflowWrap: 'anywhere',
      }}>{cmd}</code>
      <Button onClick={() => void copy(cmd)}>{t('quality.promotion.copy')}</Button>
    </div>
  )
  return (
    <div style={{ fontSize: 'var(--font-size-sm)' }}>
      <p style={{ marginTop: 0 }}>{t('quality.promotion.why')}</p>
      {waiting.length === 0 && (
        <>
          <p style={{ color: 'var(--color-muted)' }}>{t('quality.promotion.noneWaiting')}</p>
          {commandBlock(promotionCommand(null, placeholders))}
        </>
      )}
      {waiting.map(e => (
        <div key={e.experiment_id} style={{ marginTop: 'var(--space-3)' }}>
          {/* lab text: plain text only */}
          <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>{e.hypothesis ?? e.candidate_id}</div>
          {e.promotion_evidence === 'exploratory_only' && (
            <div style={{ color: 'var(--color-warning)' }}>{t('quality.promotion.exploratory')}</div>
          )}
          {commandBlock(promotionCommand(e, placeholders))}
        </div>
      ))}
      <p style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-xs)', marginBottom: 0 }}>{t('quality.promotion.how')}</p>
    </div>
  )
}

function RunsSection() {
  const { t } = useTranslation()
  const runs = useQuery({ queryKey: ['quality-pipeline-runs'], queryFn: () => listRuns(10) })
  const columns: DataTableColumn<PipelineRun>[] = [
    { key: 'when', header: t('quality.runs.columns.when'), render: r => formatDateTime(r.started_at) },
    { key: 'status', header: t('quality.runs.columns.status'), render: r => <StatusBadge status={r.status} /> },
    { key: 'result', header: t('quality.runs.columns.result'),
      render: r => t('quality.runs.result', { found: r.stats?.collected ?? 0, kept: r.stats?.scored ?? 0 }) },
  ]
  return (
    <Card title={t('quality.runs.title')} subtitle={t('quality.runs.subtitle')} style={{ marginBottom: 'var(--space-4)' }}
      actions={<Link to="/pipeline">{t('quality.runs.funnelLink')}</Link>}>
      {runs.isError
        ? <ErrorState message={t('quality.runs.error')} onRetry={() => void runs.refetch()} />
        : <DataTable columns={columns} rows={runs.data} rowKey={r => r.id} loading={runs.isLoading} emptyMessage={t('quality.runs.empty')} />}
    </Card>
  )
}

export default function Quality() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [advanced, setAdvanced] = useState(false)
  const [startedId, setStartedId] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const reported = useRef<Set<string>>(new Set())

  const health = useQuery({ queryKey: ['quality-health'], queryFn: getQualityHealth })
  const jobs = useQuery({ queryKey: ['quality-jobs'], queryFn: () => listQualityJobs(20) })
  const experiments = useQuery({ queryKey: ['lab-experiments'], queryFn: getLabExperiments })
  const activeId = startedId ?? jobs.data?.active_id ?? null
  const job = useQuery({
    queryKey: ['quality-job', activeId], queryFn: () => getQualityJob(activeId as string), enabled: activeId !== null,
    refetchInterval: q => (q.state.data && q.state.data.status !== 'running' ? false : 1000),
  })
  const running = job.data?.status === 'running' ? job.data : null

  // A job that just finished: say so once, refresh what it changed, and free the buttons.
  useEffect(() => {
    const j = job.data
    if (!j || j.status === 'running' || reported.current.has(j.id)) return
    reported.current.add(j.id)
    if (startedId === j.id) {
      if (j.status === 'completed') toast.show({ message: t(`quality.toast.done.${j.kind}`), tone: 'success' })
      else if (j.status === 'cancelled') toast.show({ message: t('quality.toast.cancelled'), tone: 'info' })
      else toast.show({ message: t(`quality.errors.${j.error_code && KNOWN_ERRORS.has(j.error_code) ? j.error_code : 'unexpected'}`), tone: 'danger' })
    }
    setStartedId(null)
    setCancelling(false)
    void queryClient.invalidateQueries({ queryKey: ['quality-health'] })
    void queryClient.invalidateQueries({ queryKey: ['quality-jobs'] })
    void queryClient.invalidateQueries({ queryKey: ['lab-self-check'] })
    void queryClient.invalidateQueries({ queryKey: ['lab-runs'] })
  }, [job.data, startedId, queryClient, t, toast])

  const start = useMutation({
    mutationFn: (kind: QualityJobKind) => startQualityJob(kind),
    onSuccess: j => { setStartedId(j.id); void queryClient.invalidateQueries({ queryKey: ['quality-jobs'] }) },
    onError: err => {
      const code = errorCodeOf(err)
      toast.show({ message: t(code === 'busy' ? 'quality.errors.busy' : 'quality.errors.startFailed'), tone: code === 'busy' ? 'warning' : 'danger' })
      void queryClient.invalidateQueries({ queryKey: ['quality-jobs'] })
    },
  })
  const cancel = useMutation({
    mutationFn: (id: string) => cancelQualityJob(id),
    onMutate: () => setCancelling(true),
    onError: () => { setCancelling(false); toast.show({ message: t('quality.errors.cancelFailed'), tone: 'danger' }) },
  })

  const busy = running !== null || start.isPending
  const historyColumns: DataTableColumn<QualityJob>[] = [
    { key: 'when', header: t('quality.history.columns.when'), render: j => formatDateTime(j.created_at) },
    { key: 'what', header: t('quality.history.columns.what'), render: j => t(`quality.kinds.${j.kind}`) },
    { key: 'status', header: t('quality.history.columns.status'),
      render: j => <Chip tone={JOB_TONE[j.status] ?? 'default'}>{t(`quality.jobStatus.${j.status}`)}</Chip> },
    { key: 'result', header: t('quality.history.columns.result'), render: j => shortResult(j, t) },
  ]

  return (
    <div>
      <h1 style={{ marginBottom: 'var(--space-1)' }}>{t('quality.title')}</h1>
      <p style={{ color: 'var(--color-muted)', marginTop: 0 }}>{t('quality.intro')}</p>

      <Card title={t('quality.health.title')} subtitle={t('quality.health.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
        {health.isLoading && <LoadingState />}
        {health.isError && <ErrorState message={t('quality.health.error')} onRetry={() => void health.refetch()} />}
        {health.data && !health.data.self_check && !health.data.evaluation && (
          <EmptyState message={t('quality.health.empty')} action={
            <Button variant="primary" disabled={busy} onClick={() => start.mutate('self_check')}>{t('quality.actions.runSelfCheck')}</Button>
          } />
        )}
        {health.data?.self_check && <SummaryView s={health.data.self_check} />}
        {health.data?.evaluation && (
          <div style={{ marginTop: 'var(--space-4)' }}>
            <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>{t('quality.health.latestEvaluation')}</div>
            <SummaryView s={health.data.evaluation} />
          </div>
        )}
      </Card>

      <Card title={t('quality.actions.title')} subtitle={t('quality.actions.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
        {running
          ? <JobProgress job={running} cancelling={cancelling} onCancel={() => cancel.mutate(running.id)} />
          : (
            <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
              <Button variant="primary" disabled={busy} onClick={() => start.mutate('self_check')}>{t('quality.actions.runSelfCheck')}</Button>
              <Button disabled={busy} onClick={() => start.mutate('evaluation')}>{t('quality.actions.runEvaluation')}</Button>
            </div>
          )}
        <p style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-xs)', marginBottom: 0 }}>{t('quality.actions.note')}</p>
      </Card>

      <Card title={t('quality.history.title')} style={{ marginBottom: 'var(--space-4)' }}>
        {jobs.isError
          ? <ErrorState message={t('quality.history.error')} onRetry={() => void jobs.refetch()} />
          : <DataTable columns={historyColumns} rows={jobs.data?.jobs} rowKey={j => j.id} loading={jobs.isLoading} emptyMessage={t('quality.history.empty')} />}
      </Card>

      <CalibrationCard />

      <RunsSection />

      <Card title={t('quality.promotion.title')} style={{ marginBottom: 'var(--space-4)' }}>
        {experiments.isLoading && <LoadingState />}
        {experiments.isError && <ErrorState message={t('quality.promotion.error')} onRetry={() => void experiments.refetch()} />}
        {experiments.data && <PromotionSection experiments={experiments.data.experiments} />}
      </Card>

      <Card style={{ marginBottom: 'var(--space-4)' }}>
        <Switch checked={advanced} onChange={setAdvanced} label={t('quality.advanced.label')} description={t('quality.advanced.description')} />
      </Card>
      {advanced && <Lab embedded />}
    </div>
  )
}
