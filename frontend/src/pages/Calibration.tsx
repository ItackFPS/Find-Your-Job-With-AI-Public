import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  answerCalibration, extendCalibration, getCalibration, importCalibration, nextCalibrationItems, saveCalibration,
  type CalibrationAnswer, type CalibrationDataset, type CalibrationItem,
} from '../api/client'
import { Button, Card, Chip, EmptyState, ErrorState, LoadingState, ProgressBar, useToast } from '../components'

// Quality › Calibration (completion plan P2.3): the owner labels offers of an exported dataset, blind.
// An item shows the offer's own text only — never a score, tier, decision or machine proposal — so the
// labels stay independent evidence. Offer text is rendered as plain text.

const ANSWERS: CalibrationAnswer[] = ['relevant', 'not_relevant', 'not_sure']
const SESSION_SIZES = [10, 20, 30]
const KNOWN_ERRORS = new Set(['invalid_dataset', 'not_real', 'already_imported', 'not_found', 'already_labelled'])

function errorCode(err: unknown): string {
  const code = (err as { response?: { data?: { detail?: { code?: string } } } })?.response?.data?.detail?.code
  return code && KNOWN_ERRORS.has(code) ? code : 'unexpected'
}

// Presentational: one offer to label, and the answer buttons.
export function CalibrationItemView({ item, index, count, reasons, busy, onAnswer, onSkip, onFinish }: {
  item: CalibrationItem; index: number; count: number; reasons: string[]; busy: boolean
  onAnswer: (answer: CalibrationAnswer, reason: string | null) => void; onSkip: () => void; onFinish: () => void
}) {
  const { t } = useTranslation()
  const [reason, setReason] = useState<string | null>(null)
  const meta = [item.company, item.location, item.remote ? t('quality.calibration.remote') : null].filter(Boolean).join(' · ')
  const answer = (a: CalibrationAnswer) => { onAnswer(a, reason); setReason(null) }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
      <div style={{ color: 'var(--color-muted)', fontSize: 'var(--font-size-xs)' }}>
        {t('quality.calibration.itemOf', { index: index + 1, count })}
      </div>
      <div>
        <div style={{ fontWeight: 'var(--font-weight-semibold)', fontSize: 'var(--font-size-lg)' }}>{item.title}</div>
        {meta && <div style={{ color: 'var(--color-muted)' }}>{meta}</div>}
      </div>
      <div style={{
        maxHeight: '22rem', overflowY: 'auto', whiteSpace: 'pre-wrap', padding: 'var(--space-3)',
        border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)', fontSize: 'var(--font-size-sm)',
      }}>
        {item.description || t('quality.calibration.noDescription')}
        {item.description_truncated && <div style={{ color: 'var(--color-muted)' }}>{t('quality.calibration.truncated')}</div>}
      </div>
      <div>
        <div style={{ fontSize: 'var(--font-size-sm)', marginBottom: 'var(--space-1)' }}>{t('quality.calibration.reasonPrompt')}</div>
        <div style={{ display: 'flex', gap: 'var(--space-1)', flexWrap: 'wrap' }}>
          {reasons.map(r => (
            <button key={r} type="button" aria-pressed={reason === r} onClick={() => setReason(reason === r ? null : r)}
              style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer' }}>
              <Chip tone={reason === r ? 'info' : 'default'}>{t(`quality.calibration.reasons.${r}`)}</Chip>
            </button>
          ))}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
        {ANSWERS.map(a => (
          <Button key={a} variant={a === 'relevant' ? 'primary' : 'secondary'} disabled={busy} onClick={() => answer(a)}>
            {t(`quality.calibration.answers.${a}`)}
          </Button>
        ))}
        <Button variant="ghost" disabled={busy} onClick={() => { setReason(null); onSkip() }}>{t('quality.calibration.skip')}</Button>
        <Button variant="ghost" disabled={busy} onClick={onFinish}>{t('quality.calibration.finish')}</Button>
      </div>
    </div>
  )
}

// Presentational: progress towards the labelling goal.
export function CalibrationProgress({ d }: { d: CalibrationDataset }) {
  const { t } = useTranslation()
  const labelled = d.labelled ?? 0
  const target = d.target ?? 100
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)', fontSize: 'var(--font-size-sm)' }}>
      <ProgressBar value={target ? (100 * Math.min(labelled, target)) / target : 0}
        label={t('quality.calibration.progress', { labelled, target, dataset: d.dataset_id })}
        rightLabel={t('quality.calibration.ofTotal', { total: d.total ?? 0 })} />
      {labelled > 0 && d.by_answer && (
        <div style={{ color: 'var(--color-muted)' }}>{t('quality.calibration.counts', {
          relevant: d.by_answer.relevant, notRelevant: d.by_answer.not_relevant, notSure: d.by_answer.not_sure,
        })}</div>
      )}
    </div>
  )
}

export default function CalibrationCard() {
  const { t } = useTranslation()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [source, setSource] = useState('')
  const [session, setSession] = useState<{ id: string; items: CalibrationItem[]; index: number } | null>(null)
  const overview = useQuery({ queryKey: ['calibration'], queryFn: getCalibration })
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['calibration'] })
  const fail = (err: unknown) => toast.show({ message: t(`quality.calibration.errors.${errorCode(err)}`), tone: 'danger' })

  const importer = useMutation({ mutationFn: importCalibration, onSuccess: () => { setSource(''); refresh() }, onError: fail })
  const start = useMutation({
    mutationFn: ({ id, n }: { id: string; n: number }) => nextCalibrationItems(id, n).then(items => ({ id, items })),
    onSuccess: ({ id, items }) => {
      if (items.length === 0) toast.show({ message: t('quality.calibration.nothingLeft'), tone: 'info' })
      else setSession({ id, items, index: 0 })
    },
    onError: fail,
  })
  const save = useMutation({
    mutationFn: saveCalibration,
    onSuccess: r => {
      setSession(null)
      refresh()
      toast.show({ message: t('quality.calibration.saved', { count: r.changed, version: r.version }), tone: 'success' })
    },
    onError: fail,
  })
  const answer = useMutation({
    mutationFn: ({ a, reason }: { a: CalibrationAnswer; reason: string | null }) =>
      answerCalibration(session!.id, session!.items[session!.index].job_id, a, reason),
    onSuccess: () => advance(),
    onError: fail,
  })
  const extend = useMutation({ mutationFn: extendCalibration, onSuccess: refresh, onError: fail })

  function advance() {
    if (!session) return
    if (session.index + 1 < session.items.length) setSession({ ...session, index: session.index + 1 })
    else save.mutate(session.id)
  }

  const busy = answer.isPending || save.isPending
  const data = overview.data
  return (
    <Card title={t('quality.calibration.title')} subtitle={t('quality.calibration.subtitle')} style={{ marginBottom: 'var(--space-4)' }}>
      {overview.isLoading && <LoadingState />}
      {overview.isError && <ErrorState message={t('quality.calibration.errors.load')} onRetry={() => void overview.refetch()} />}
      {session && data && (
        <CalibrationItemView key={session.items[session.index].job_id} item={session.items[session.index]}
          index={session.index} count={session.items.length} reasons={data.reasons} busy={busy}
          onAnswer={(a, reason) => answer.mutate({ a, reason })} onSkip={advance} onFinish={() => save.mutate(session.id)} />
      )}
      {!session && data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)', fontSize: 'var(--font-size-sm)' }}>
          <p style={{ margin: 0 }}>{t('quality.calibration.why')}</p>
          {data.datasets.map(d => d.error ? (
            <div key={d.dataset_id} style={{ color: 'var(--color-error)' }}>{t('quality.calibration.errors.invalid_dataset')} ({d.dataset_id})</div>
          ) : (
            <div key={d.dataset_id} style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
              <CalibrationProgress d={d} />
              <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                {(d.labelled ?? 0) < (d.total ?? 0) && SESSION_SIZES.map((n, i) => (
                  <Button key={n} variant={i === 0 ? 'primary' : 'secondary'} disabled={start.isPending}
                    onClick={() => start.mutate({ id: d.dataset_id, n })}>{t('quality.calibration.start', { count: n })}</Button>
                ))}
                {!!d.pending && (
                  <Button disabled={save.isPending} onClick={() => save.mutate(d.dataset_id)}>
                    {t('quality.calibration.savePending', { count: d.pending })}
                  </Button>
                )}
                {(d.labelled ?? 0) >= (d.target ?? 0) && (d.target ?? 0) < (d.total ?? 0) && (
                  <Button disabled={extend.isPending} onClick={() => extend.mutate(d.dataset_id)}>{t('quality.calibration.more')}</Button>
                )}
              </div>
            </div>
          ))}
          {data.datasets.length === 0 && data.exports_found.length === 0 && <EmptyState message={t('quality.calibration.empty')} />}
          {data.exports_found.map(e => (
            <div key={e.source} style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
              <span>{t('quality.calibration.found', { dataset: e.dataset_id, total: e.total })}</span>
              <Button disabled={importer.isPending} onClick={() => importer.mutate(e.source)}>{t('quality.calibration.import')}</Button>
            </div>
          ))}
          <form onSubmit={ev => { ev.preventDefault(); if (source.trim()) importer.mutate(source.trim()) }}
            style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', alignItems: 'end' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)', flex: '1 1 20rem' }}>
              {t('quality.calibration.folderLabel')}
              <input value={source} onChange={ev => setSource(ev.target.value)} placeholder={t('quality.calibration.folderPlaceholder')}
                style={{ padding: 'var(--space-2)', borderRadius: 'var(--radius-md)', border: '1px solid var(--color-border)' }} />
            </label>
            <Button type="submit" disabled={importer.isPending || !source.trim()}>{t('quality.calibration.import')}</Button>
          </form>
          <p style={{ margin: 0, color: 'var(--color-muted)', fontSize: 'var(--font-size-xs)' }}>{t('quality.calibration.note')}</p>
        </div>
      )}
    </Card>
  )
}
