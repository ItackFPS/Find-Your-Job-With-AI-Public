import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'

export type ToastTone = 'info' | 'success' | 'warning' | 'danger'

export interface ToastOptions {
  message: ReactNode
  tone?: ToastTone
  /** Milliseconds before it closes by itself; 0 keeps it until dismissed. Danger toasts default to 0. */
  duration?: number
}

export interface ToastContextValue {
  show: (toast: ToastOptions) => number
  dismiss: (id: number) => void
}

export const ToastContext = createContext<ToastContextValue | null>(null)

/** Show transient messages ("Saved", "Run started") from anywhere under <ToastProvider>. */
export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}

