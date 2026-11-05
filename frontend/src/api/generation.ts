// Erasure generation token (ADR 0015): "<data_epoch>:<generation>". Every API response carries it
// in the X-Erasure-Generation header; the client stamps each personal write with the value it held
// when the user acted, so a write from before an erasure is refused (409) instead of resurrecting
// erased activity.

export const GENERATION_HEADER = 'x-erasure-generation'

let current: string | null = null
const listeners = new Set<(token: string | null) => void>()

export function getGenerationToken(): string | null {
  return current
}

export function setGenerationToken(token: string | null | undefined): void {
  const next = token ?? null
  if (next === current) return
  current = next
  listeners.forEach(fn => fn(next))
}

export function onGenerationChange(fn: (token: string | null) => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function dataEpochOf(token: string | null): string | null {
  if (!token) return null
  const i = token.lastIndexOf(':')
  return i > 0 ? token.slice(0, i) : null
}
