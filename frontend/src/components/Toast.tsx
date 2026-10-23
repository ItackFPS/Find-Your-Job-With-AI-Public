import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Button } from './Button'
import { ToastContext, type ToastOptions } from './toastContext'

interface ToastItem extends Required<Omit<ToastOptions, 'duration'>> {
  id: number
  duration: number
}


function ToastView({ toast, dismiss, dismissLabel }: { toast: ToastItem; dismiss: (id: number) => void; dismissLabel: string }) {
  useEffect(() => {
    if (toast.duration <= 0) return
    const timer = window.setTimeout(() => dismiss(toast.id), toast.duration)
    return () => window.clearTimeout(timer)
  }, [toast.id, toast.duration, dismiss])

  return (
    <div className={`fyj-toast fyj-toast--${toast.tone}`} role={toast.tone === 'danger' ? 'alert' : 'status'}>
      <div className="fyj-toast__body">{toast.message}</div>
      <Button variant="ghost" aria-label={dismissLabel} onClick={() => dismiss(toast.id)}>×</Button>
    </div>
  )
}

/**
 * Owns the toast list and renders the toast region: a polite live region in the bottom
 * right corner. Danger toasts are announced assertively and stay until dismissed.
 */
export function ToastProvider({ children, dismissLabel = 'Dismiss notification', regionLabel = 'Notifications' }: {
  children: ReactNode
  dismissLabel?: string
  regionLabel?: string
}) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => setToasts(list => list.filter(t => t.id !== id)), [])
  const show = useCallback((options: ToastOptions) => {
    const id = nextId.current++
    const tone = options.tone ?? 'info'
    const duration = options.duration ?? (tone === 'danger' ? 0 : 5000)
    setToasts(list => [...list, { id, message: options.message, tone, duration }])
    return id
  }, [])
  const value = useMemo(() => ({ show, dismiss }), [show, dismiss])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <section className="fyj-toast-region" aria-label={regionLabel}>
        {toasts.map(t => (
          <ToastView key={t.id} toast={t} dismissLabel={dismissLabel} dismiss={dismiss} />
        ))}
      </section>
    </ToastContext.Provider>
  )
}
