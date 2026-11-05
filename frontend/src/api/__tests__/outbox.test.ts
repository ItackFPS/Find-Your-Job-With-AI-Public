import { describe, expect, it, vi } from 'vitest'
import { MemoryStore, Outbox, browserIdentity, type OutboxEntry, type SendResult } from '../outbox'
import { setGenerationToken } from '../generation'

function makeOutbox(responses: Array<number | 'network'>, extra: Partial<ConstructorParameters<typeof Outbox>[0]> = {}) {
  const store = new MemoryStore()
  const sent: OutboxEntry[] = []
  const scheduled: Array<() => void> = []
  let seq = 0
  let id = 0
  const transport = vi.fn(async (e: OutboxEntry): Promise<SendResult> => {
    sent.push(e)
    const r = responses.shift() ?? 201
    if (r === 'network') throw new Error('offline')
    return { status: r, data: { event_id: `srv-${e.client_event_id}` } }
  })
  const outbox = new Outbox({
    store, transport, instanceId: 'inst-A', nextSeq: () => ++seq, newId: () => `evt-${++id}`,
    schedule: fn => { scheduled.push(fn) }, ...extra,
  })
  return { outbox, store, sent, scheduled, transport }
}

describe('Outbox', () => {
  it('acknowledges, removes the entry and stamps delivery fields', async () => {
    setGenerationToken('epoch-1:0')
    const { outbox, store, sent } = makeOutbox([201])
    const { done } = await outbox.enqueue('j1', { type: 'saved' })
    expect(await done).toEqual({ kind: 'acked', data: { event_id: 'srv-evt-1' } })
    expect(await store.all()).toEqual([])
    expect(sent[0]).toMatchObject({ job_id: 'j1', generation: 'epoch-1:0', client_instance_id: 'inst-A', client_seq: 1 })
  })

  it('keeps the entry on a network failure and resends the same client_event_id', async () => {
    const { outbox, store, sent, scheduled } = makeOutbox(['network', 201])
    const { done } = await outbox.enqueue('j1', { type: 'hidden' })
    await outbox.idle()
    expect((await store.all()).map(e => e.attempts)).toEqual([1])
    expect(scheduled).toHaveLength(1)
    scheduled[0]()
    expect((await done).kind).toBe('acked')
    expect(sent.map(e => e.client_event_id)).toEqual(['evt-1', 'evt-1'])
    expect(await store.all()).toEqual([])
  })

  it('drops an entry refused for an erased generation (409)', async () => {
    const { outbox, store } = makeOutbox([409])
    const { done } = await outbox.enqueue('j1', { type: 'saved' })
    expect(await done).toEqual({ kind: 'dropped', reason: 'erased', status: 409 })
    expect(await store.all()).toEqual([])
  })

  it('keeps an entry while an erasure runs (423)', async () => {
    const { outbox, store, scheduled } = makeOutbox([423])
    await outbox.enqueue('j1', { type: 'saved' })
    await outbox.idle()
    expect(await store.all()).toHaveLength(1)
    expect(scheduled).toHaveLength(1)
  })

  it('sends one offer in sequence order and stops at the first failure', async () => {
    const { outbox, sent, store } = makeOutbox([503, 503, 503])
    await outbox.enqueue('j1', { type: 'hidden' })
    await outbox.enqueue('j1', { type: 'unhidden' })
    await outbox.idle()
    // the unhide never overtakes the hide
    expect(sent.every(e => e.body.type === 'hidden')).toBe(true)
    expect((await store.all()).map(e => e.body.type).sort()).toEqual(['hidden', 'unhidden'])
  })

  it('uses Web Locks per offer when available', async () => {
    const names: string[] = []
    const locks = { request: async (name: string, fn: () => Promise<void>) => { names.push(name); await fn() } }
    const { outbox } = makeOutbox([201], { locks })
    const { done } = await outbox.enqueue('j7', { type: 'saved' })
    await done
    expect(names).toContain('fyj-outbox:j7')
  })

  it('reports a memory store as not durable', () => {
    const { outbox } = makeOutbox([])
    expect(outbox.durable).toBe(false)
  })
})

describe('browserIdentity', () => {
  it('keeps the instance id and a growing sequence across calls', () => {
    const data = new Map<string, string>()
    const storage = {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => { data.set(k, v) },
    } as unknown as Storage
    const a = browserIdentity(storage)
    expect(a.nextSeq()).toBe(1)
    expect(a.nextSeq()).toBe(2)
    const b = browserIdentity(storage)
    expect(b.instanceId).toBe(a.instanceId)
    expect(b.nextSeq()).toBe(3)
  })

  it('works without storage', () => {
    const id = browserIdentity(null)
    expect(id.instanceId).toBeTruthy()
    expect(id.nextSeq()).toBe(1)
  })
})
