import { useEffect, useRef, useState } from 'react'
import { PIPELINE_EVENTS_URL, type PipelineEvent, type RunStats } from '../api/client'
import { stageLabel } from '../config/pipelineStages'
import { formatTime } from '../utils/formatDate'

// ---------------------------------------------------------------------------
// Live pipeline progress (SSE) — observational only; the backend/pipeline
// remains the source of truth, this just displays what it publishes.
//
// Extracted verbatim from Dashboard.tsx (Phase 1 frontend refactor) — no
// behavior change: same EventSource setup, reconnection handling, event
// parsing, coalescing and cleanup as before.
// ---------------------------------------------------------------------------

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline'

export interface ActivityEntry {
  id: number
  time: string
  text: string
}

// Stage labels now live in src/config/pipelineStages.ts (the one canonical
// stage definition Dashboard/Pipeline also consume) — re-exported here so
// existing imports of `stageLabel` from this hook keep working unchanged.
export { stageLabel } from '../config/pipelineStages'

// Which RunStats field represents "how many jobs this stage has produced so
// far" — used only for the activity log line, never to invent a stat that
// doesn't already exist on RunStats.
const STAGE_COUNT_FIELD: Partial<Record<string, keyof RunStats>> = {
  collect: 'collected',
  validate: 'validated',
  deduplicate: 'deduplicated',
  filter: 'filtered',
  requirements_gate: 'filtered',
  score: 'scored',
  semantic: 'semantically_matched',
  personalize: 'personalized',
}

const LIVE_EVENT_TYPES: PipelineEvent['type'][] = [
  'pipeline.started', 'pipeline.progress', 'pipeline.completed', 'pipeline.failed', 'pipeline.snapshot',
]

function eventActivityText(ev: PipelineEvent): string {
  if (ev.type === 'pipeline.started') return 'Pipeline started'
  if (ev.type === 'pipeline.completed') return 'Pipeline completed'
  if (ev.type === 'pipeline.failed') return `Pipeline failed${ev.error ? ` — ${ev.error}` : ''}`
  const label = stageLabel(ev.stage)
  if (ev.type === 'pipeline.snapshot') return `Reconnected — ${label}`
  const field = STAGE_COUNT_FIELD[ev.stage]
  const count = field ? (ev.stats[field] as number | undefined) : undefined
  return typeof count === 'number' ? `${label} — ${count.toLocaleString()} jobs` : label
}

export function usePipelineEvents() {
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [liveEvent, setLiveEvent] = useState<PipelineEvent | null>(null)
  const [activity, setActivity] = useState<ActivityEntry[]>([])
  const errorStreak = useRef(0)
  const activityId = useRef(0)
  const trackedRunId = useRef<string | null>(null)
  const lastStage = useRef<string | null>(null)

  useEffect(() => {
    let closed = false
    const source = new EventSource(PIPELINE_EVENTS_URL)

    source.onopen = () => {
      if (closed) return
      errorStreak.current = 0
      setConnection('live')
    }

    // The browser's EventSource retries automatically on error/disconnect —
    // we only need to reflect that as UI state, never reset counters or
    // mark the run as failed just because the connection blipped.
    source.onerror = () => {
      if (closed) return
      errorStreak.current += 1
      setConnection(errorStreak.current >= 4 ? 'offline' : 'reconnecting')
    }

    const handle = (raw: MessageEvent) => {
      if (closed) return
      let ev: PipelineEvent
      try {
        ev = JSON.parse(raw.data)
      } catch {
        return
      }

      // A late/stale event from a superseded run must not corrupt the
      // display — only accept updates for the run currently being tracked,
      // unless this event itself (re-)anchors that tracking.
      const anchors = ev.type === 'pipeline.started' || ev.type === 'pipeline.snapshot'
      if (!anchors && trackedRunId.current !== null && ev.run_id !== trackedRunId.current) {
        return
      }
      trackedRunId.current = ev.run_id
      setLiveEvent(ev)

      const time = formatTime(ev.timestamp || new Date().toISOString())
      const entry: ActivityEntry = { id: activityId.current++, time, text: eventActivityText(ev) }

      setActivity(prev => {
        // Coalesce consecutive same-stage progress updates (Phase 2 fires
        // frequent sub-progress events) into one live-updating line instead
        // of flooding the log.
        if (prev.length > 0 && ev.type === 'pipeline.progress' && lastStage.current === ev.stage) {
          lastStage.current = ev.stage
          return [...prev.slice(0, -1), entry]
        }
        lastStage.current = ev.stage
        return [...prev.slice(-19), entry]
      })
    }

    LIVE_EVENT_TYPES.forEach(t => source.addEventListener(t, handle))

    return () => {
      closed = true
      LIVE_EVENT_TYPES.forEach(t => source.removeEventListener(t, handle))
      source.close()
    }
  }, [])

  return { connection, liveEvent, activity }
}
