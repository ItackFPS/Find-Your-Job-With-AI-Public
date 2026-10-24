import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { ToastProvider } from '../Toast'
import { useToast, type ToastOptions } from '../toastContext'

function Trigger(props: ToastOptions) {
  const toast = useToast()
  return <button onClick={() => toast.show(props)}>show</button>
}

describe('Toast', () => {
  afterEach(() => vi.useRealTimers())

  it('shows a polite status toast that closes by itself', () => {
    vi.useFakeTimers()
    render(<ToastProvider><Trigger message="Saved" tone="success" /></ToastProvider>)
    act(() => screen.getByText('show').click())
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
  })

  it('keeps a danger toast as an alert until it is dismissed', () => {
    vi.useFakeTimers()
    render(<ToastProvider><Trigger message="Failed" tone="danger" /></ToastProvider>)
    act(() => screen.getByText('show').click())
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(screen.getByRole('alert')).toHaveTextContent('Failed')
    act(() => screen.getByRole('button', { name: 'Dismiss notification' }).click())
    expect(screen.queryByText('Failed')).not.toBeInTheDocument()
  })

  it('throws a clear error outside the provider', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<Trigger message="x" />)).toThrow(/ToastProvider/)
  })
})
