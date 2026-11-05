import { useState } from 'react'
import type { MouseEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { getOutbox } from '../api/feedbackOutbox'
import type { Outbox } from '../api/outbox'
import { Button } from './Button'

type Pending = 'save' | 'hide' | null

export interface SaveHideButtonsProps {
  jobId: string
  saved?: boolean
  hidden?: boolean
  /** Where the action was taken (feedback context, ADR 0016). */
  surface: string
  position?: number
  /** Called once the server acknowledged the new hidden state. */
  onHiddenChange?: (hidden: boolean) => void
  /** Tests inject an outbox; the application uses the shared one. */
  outbox?: Promise<Outbox>
}

/**
 * Save and Hide (ADR 0016, ADR 0017). The state shown changes only after the server acknowledged
 * the action ("Saved" never appears for an action still in the outbox). An action refused because
 * the activity was erased (409) is dropped and said so; one waiting on an erasure (423) or the
 * network stays queued and is retried.
 */
export function SaveHideButtons({ jobId, saved = false, hidden = false, surface, position, onHiddenChange, outbox }: SaveHideButtonsProps) {
  const { t } = useTranslation()
  const [isSaved, setSaved] = useState(saved)
  const [isHidden, setHidden] = useState(hidden)
  const [pending, setPending] = useState<Pending>(null)
  const [message, setMessage] = useState<string | null>(null)

  const send = async (kind: 'save' | 'hide', type: string, apply: () => void) => {
    setPending(kind)
    setMessage(null)
    try {
      const box = await (outbox ?? getOutbox())
      const context: Record<string, string | number> = { surface }
      if (position !== undefined) context.position = position
      const { done } = await box.enqueue(jobId, { type, reasons: [], context })
      if (!box.durable) setMessage(t('saveHide.notDurable'))
      const outcome = await done
      if (outcome.kind === 'acked') apply()
      else setMessage(t(outcome.reason === 'erased' ? 'saveHide.erased' : 'saveHide.rejected'))
    } catch {
      setMessage(t('saveHide.failed'))
    } finally {
      setPending(null)
    }
  }

  const stop = (e: MouseEvent) => { e.preventDefault(); e.stopPropagation() }

  const toggleSave = (e: MouseEvent) => {
    stop(e)
    const next = !isSaved
    void send('save', next ? 'saved' : 'unsaved', () => setSaved(next))
  }
  const toggleHide = (e: MouseEvent) => {
    stop(e)
    const next = !isHidden
    void send('hide', next ? 'hidden' : 'unhidden', () => { setHidden(next); onHiddenChange?.(next) })
  }

  return (
    <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }} onClick={stop}>
      <Button variant="ghost" onClick={toggleSave} disabled={pending !== null} aria-pressed={isSaved}>
        {pending === 'save' ? t('saveHide.sending') : isSaved ? t('saveHide.saved') : t('saveHide.save')}
      </Button>
      <Button variant="ghost" onClick={toggleHide} disabled={pending !== null} aria-pressed={isHidden}>
        {pending === 'hide' ? t('saveHide.sending') : isHidden ? t('saveHide.unhide') : t('saveHide.hide')}
      </Button>
      {message && <span role="status" style={{ fontSize: '0.78rem', color: 'var(--color-muted)' }}>{message}</span>}
    </div>
  )
}
