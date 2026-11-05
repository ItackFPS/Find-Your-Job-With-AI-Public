import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SaveHideButtons } from '../SaveHideButtons'
import { MemoryStore, Outbox, type SendResult } from '../../api/outbox'

function outboxReturning(...statuses: number[]) {
  let resolveSend: ((r: SendResult) => void) | null = null
  const gate = { release: () => resolveSend?.({ status: statuses.shift() ?? 201 }) }
  const outbox = new Outbox({
    store: new MemoryStore(), instanceId: 'i', nextSeq: (() => { let s = 0; return () => ++s })(),
    transport: () => new Promise<SendResult>(r => { resolveSend = r }), schedule: () => {},
  })
  return { outbox: Promise.resolve(outbox), gate }
}

describe('SaveHideButtons', () => {
  it('shows "Saved" only after the server acknowledged', async () => {
    const { outbox, gate } = outboxReturning(201)
    render(<SaveHideButtons jobId="j1" surface="jobs" outbox={outbox} />)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('button', { name: 'Sending…' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Saved' })).not.toBeInTheDocument()
    gate.release()
    expect(await screen.findByRole('button', { name: 'Saved' })).toBeInTheDocument()
  })

  it('says an action was not recorded when the activity was erased (409)', async () => {
    const { outbox, gate } = outboxReturning(409)
    render(<SaveHideButtons jobId="j1" surface="jobs" outbox={outbox} />)
    await userEvent.click(screen.getByRole('button', { name: 'Hide' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sending…' })).toBeInTheDocument())
    gate.release()
    expect(await screen.findByText(/activity was erased/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide' })).toBeInTheDocument()
  })

  it('reports the hidden state once acknowledged', async () => {
    const { outbox, gate } = outboxReturning(201)
    const onHiddenChange = vi.fn()
    render(<SaveHideButtons jobId="j1" surface="jobs" outbox={outbox} onHiddenChange={onHiddenChange} />)
    await userEvent.click(screen.getByRole('button', { name: 'Hide' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sending…' })).toBeInTheDocument())
    expect(onHiddenChange).not.toHaveBeenCalled()
    gate.release()
    await waitFor(() => expect(onHiddenChange).toHaveBeenCalledWith(true))
  })
})
