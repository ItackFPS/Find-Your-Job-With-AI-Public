// Durable, acknowledged, ordered delivery of personal actions (ADR 0016).
//
// Every durable action (Save, Hide, explicit feedback) is written to an outbox before it is sent,
// with a client_event_id (the server stores a replay once), the erasure generation token held when
// the user acted (ADR 0015), and a per-browser-profile sequence number (client_instance_id,
// client_seq) so a late, older state change is marked superseded server-side.
//
// Delivery: entries of one offer go out in sequence order, one at a time (Web Locks across tabs when
// the browser has them). 2xx acknowledges and removes; 409 (erased generation) drops; 423 (erasure
// running) and network/5xx keep the entry and retry later; other 4xx drop as rejected. The outbox
// lives in IndexedDB; without it, an in-memory store is used and `durable` is false so the UI warns.

import { getGenerationToken } from './generation'

export interface OutboxBody {
  type: string
  reasons?: string[]
  note?: string
  decision_id?: string
  context?: Record<string, string | number>
}

export interface OutboxEntry {
  client_event_id: string
  job_id: string
  body: OutboxBody
  generation: string | null
  client_instance_id: string
  client_seq: number
  client_created_at: string
  attempts: number
}

export interface OutboxStore {
  readonly durable: boolean
  put(entry: OutboxEntry): Promise<void>
  remove(id: string): Promise<void>
  all(): Promise<OutboxEntry[]>
}

export type Outcome =
  | { kind: 'acked'; data: unknown }
  | { kind: 'dropped'; reason: 'erased' | 'rejected'; status: number }

export interface SendResult { status: number; data?: unknown }
/** Sends one entry; resolves with the HTTP status, throws on a network failure. */
export type Transport = (entry: OutboxEntry) => Promise<SendResult>

interface LockLike { request(name: string, fn: () => Promise<void>): Promise<unknown> }

export interface OutboxOptions {
  store: OutboxStore
  transport: Transport
  instanceId: string
  nextSeq: () => number
  now?: () => Date
  newId?: () => string
  retryDelayMs?: (attempts: number) => number
  schedule?: (fn: () => void, ms: number) => void
  locks?: LockLike | null
}

export class MemoryStore implements OutboxStore {
  readonly durable = false
  private items = new Map<string, OutboxEntry>()
  async put(entry: OutboxEntry) { this.items.set(entry.client_event_id, { ...entry }) }
  async remove(id: string) { this.items.delete(id) }
  async all() { return [...this.items.values()].map(e => ({ ...e })) }
}

const DB_NAME = 'fyj-outbox'
const STORE = 'events'

export class IdbStore implements OutboxStore {
  readonly durable = true
  private readonly db: IDBDatabase
  private constructor(db: IDBDatabase) { this.db = db }

  static open(factory: IDBFactory | undefined = globalThis.indexedDB): Promise<IdbStore | null> {
    if (!factory) return Promise.resolve(null)
    return new Promise(resolve => {
      try {
        const req = factory.open(DB_NAME, 1)
        req.onupgradeneeded = () => {
          if (!req.result.objectStoreNames.contains(STORE)) {
            req.result.createObjectStore(STORE, { keyPath: 'client_event_id' })
          }
        }
        req.onsuccess = () => resolve(new IdbStore(req.result))
        req.onerror = () => resolve(null)
        req.onblocked = () => resolve(null)
      } catch {
        resolve(null)
      }
    })
  }

  private run<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(STORE, mode)
      const req = fn(tx.objectStore(STORE))
      tx.oncomplete = () => resolve(req.result)
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
    })
  }

  async put(entry: OutboxEntry) { await this.run('readwrite', s => s.put(entry)) }
  async remove(id: string) { await this.run('readwrite', s => s.delete(id)) }
  async all() { return (await this.run('readonly', s => s.getAll())) as OutboxEntry[] }
}

function randomId(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export class Outbox {
  private waiters = new Map<string, (o: Outcome) => void>()
  private running: Promise<void> | null = null
  private again = false
  private retryPending = false
  private readonly o: Required<Omit<OutboxOptions, 'locks'>> & { locks: LockLike | null }

  constructor(opts: OutboxOptions) {
    this.o = {
      now: () => new Date(),
      newId: randomId,
      retryDelayMs: a => Math.min(30_000, 1000 * 2 ** Math.min(a, 5)),
      schedule: (fn, ms) => { setTimeout(fn, ms) },
      ...opts,
      locks: opts.locks ?? null,
    }
  }

  get durable(): boolean { return this.o.store.durable }

  /** Writes the action to the outbox, then tries to send it. `done` settles on ack or drop. */
  async enqueue(jobId: string, body: OutboxBody): Promise<{ client_event_id: string; done: Promise<Outcome> }> {
    const entry: OutboxEntry = {
      client_event_id: this.o.newId(),
      job_id: jobId,
      body,
      generation: getGenerationToken(),
      client_instance_id: this.o.instanceId,
      client_seq: this.o.nextSeq(),
      client_created_at: this.o.now().toISOString(),
      attempts: 0,
    }
    const done = new Promise<Outcome>(resolve => this.waiters.set(entry.client_event_id, resolve))
    await this.o.store.put(entry)
    void this.flush()
    return { client_event_id: entry.client_event_id, done }
  }

  async pending(): Promise<OutboxEntry[]> { return this.o.store.all() }

  /** Resolves once no delivery pass is running. */
  async idle(): Promise<void> {
    while (this.running) await this.running
  }

  /** Sends what is pending. Concurrent calls coalesce into one more pass. */
  flush(): Promise<void> {
    if (this.running) { this.again = true; return this.running }
    this.running = (async () => {
      do {
        this.again = false
        await this.pass()
      } while (this.again)
    })().finally(() => { this.running = null })
    return this.running
  }

  private async pass(): Promise<void> {
    const entries = (await this.o.store.all()).sort((a, b) => a.client_seq - b.client_seq)
    const byJob = new Map<string, OutboxEntry[]>()
    for (const e of entries) {
      const list = byJob.get(e.job_id) ?? []
      list.push(e)
      byJob.set(e.job_id, list)
    }
    for (const [jobId, list] of byJob) {
      const work = () => this.sendInOrder(list)
      if (this.o.locks) await this.o.locks.request(`fyj-outbox:${jobId}`, work)
      else await work()
    }
  }

  private async sendInOrder(list: OutboxEntry[]): Promise<void> {
    for (const entry of list) {
      let result: SendResult | null = null
      try {
        result = await this.o.transport(entry)
      } catch {
        result = null
      }
      const status = result?.status ?? 0
      if (status >= 200 && status < 300) {
        await this.o.store.remove(entry.client_event_id)
        this.settle(entry, { kind: 'acked', data: result?.data })
        continue
      }
      if (status === 409) {
        await this.o.store.remove(entry.client_event_id)
        this.settle(entry, { kind: 'dropped', reason: 'erased', status })
        continue
      }
      if (status >= 400 && status < 500 && status !== 423 && status !== 408 && status !== 429) {
        await this.o.store.remove(entry.client_event_id)
        this.settle(entry, { kind: 'dropped', reason: 'rejected', status })
        continue
      }
      // 423 (erasure running), 408/429, 5xx or network: keep, keep order, retry later.
      await this.o.store.put({ ...entry, attempts: entry.attempts + 1 })
      this.scheduleRetry(entry.attempts + 1)
      return
    }
  }

  private settle(entry: OutboxEntry, outcome: Outcome) {
    const fn = this.waiters.get(entry.client_event_id)
    if (fn) {
      this.waiters.delete(entry.client_event_id)
      fn(outcome)
    }
  }

  private scheduleRetry(attempts: number) {
    if (this.retryPending) return
    this.retryPending = true
    this.o.schedule(() => {
      this.retryPending = false
      void this.flush()
    }, this.o.retryDelayMs(attempts))
  }
}

// ---- Browser-profile identity (client_instance_id, client_seq) -----------------------------------

const INSTANCE_KEY = 'fyj.client_instance_id'
const SEQ_KEY = 'fyj.client_seq'

export function browserIdentity(storage: Storage | null = safeLocalStorage()): {
  instanceId: string
  nextSeq: () => number
} {
  let instanceId: string | null = null
  try { instanceId = storage?.getItem(INSTANCE_KEY) ?? null } catch { instanceId = null }
  if (!instanceId) {
    instanceId = randomId()
    try { storage?.setItem(INSTANCE_KEY, instanceId) } catch { /* memory only */ }
  }
  let memSeq = 0
  const nextSeq = () => {
    let seq = memSeq
    try { seq = Math.max(seq, Number(storage?.getItem(SEQ_KEY) ?? 0) || 0) } catch { /* memory only */ }
    seq += 1
    memSeq = seq
    try { storage?.setItem(SEQ_KEY, String(seq)) } catch { /* memory only */ }
    return seq
  }
  return { instanceId, nextSeq }
}

function safeLocalStorage(): Storage | null {
  try { return globalThis.localStorage ?? null } catch { return null }
}
