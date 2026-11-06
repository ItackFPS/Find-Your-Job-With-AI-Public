// The application's single outbox (ADR 0016): IndexedDB when available, memory otherwise.

import { sendOutboxEntry } from './client'
import { IdbStore, MemoryStore, Outbox, browserIdentity, type OutboxStore } from './outbox'

type LockArg = ConstructorParameters<typeof Outbox>[0]['locks']

let instance: Promise<Outbox> | null = null

export function getOutbox(): Promise<Outbox> {
  if (!instance) {
    instance = (async () => {
      const store: OutboxStore = (await IdbStore.open()) ?? new MemoryStore()
      const { instanceId, nextSeq } = browserIdentity()
      const locks = (typeof navigator !== 'undefined' && 'locks' in navigator && navigator.locks
        ? navigator.locks : null) as unknown as LockArg
      const outbox = new Outbox({ store, transport: sendOutboxEntry, instanceId, nextSeq, locks })
      if (typeof window !== 'undefined') window.addEventListener('online', () => { void outbox.flush() })
      void outbox.flush()   // entries left by an earlier page load
      return outbox
    })()
  }
  return instance
}

/** At app start: open the outbox now, so entries left by an earlier page load (a Save or Hide made
 *  while the backend was unreachable) are delivered without waiting for the next action. */
export function startOutbox(): void {
  void getOutbox()
}

/** Tests only: forget the singleton. */
export function resetOutbox(): void {
  instance = null
}

/** "Delete all" finished (ADR 0015): drop the outbox database and this browser's local storage.
 *  The open outbox is closed first: IndexedDB would otherwise hold the deletion until the tab closes. */
export function clearLocalData(): Promise<void> {
  const current = instance
  instance = null
  try { globalThis.localStorage?.clear() } catch { /* blocked */ }
  const drop = () => {
    try { globalThis.indexedDB?.deleteDatabase('fyj-outbox') } catch { /* unavailable */ }
  }
  if (!current) {
    drop()
    return Promise.resolve()
  }
  return current.then(o => o.close(), () => undefined).then(drop)
}
