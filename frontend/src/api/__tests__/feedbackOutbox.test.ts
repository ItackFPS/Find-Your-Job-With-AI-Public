import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryStore, type OutboxEntry } from '../outbox'

// An entry left in the store by an earlier page load (e.g. a Hide made while the backend was down).
const leftover: OutboxEntry = {
  client_event_id: 'evt-left', job_id: 'job-1', body: { type: 'hidden' }, generation: null,
  client_instance_id: 'inst-A', client_seq: 1, client_created_at: '2026-11-06T12:00:00Z', attempts: 3,
}
const store = new MemoryStore()
const sent = vi.fn(async (_e: OutboxEntry) => ({ status: 201 }))

vi.mock('../outbox', async importOriginal => {
  const real = await importOriginal<typeof import('../outbox')>()
  return { ...real, IdbStore: { open: async () => store } }
})
vi.mock('../client', () => ({ sendOutboxEntry: (e: OutboxEntry) => sent(e) }))

describe('startOutbox', () => {
  afterEach(async () => {
    const { resetOutbox } = await import('../feedbackOutbox')
    resetOutbox()
  })

  it('replays entries left by an earlier page load without waiting for a new action', async () => {
    await store.put(leftover)
    const { startOutbox } = await import('../feedbackOutbox')
    startOutbox()
    await vi.waitFor(() => expect(sent).toHaveBeenCalledTimes(1))
    expect(sent.mock.calls[0][0]).toMatchObject({ client_event_id: 'evt-left', job_id: 'job-1' })
    await vi.waitFor(async () => expect(await store.all()).toEqual([]))
  })
})
