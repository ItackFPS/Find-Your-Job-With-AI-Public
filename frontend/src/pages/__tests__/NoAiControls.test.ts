import { describe, it, expect } from 'vitest'

// 2.0 ships without AI controls (ADR 0028, D3). The backend and the `use_ai` API stay for 2.1,
// but no screen may set it: an AI run without a configured model would silently do nothing.
const sources = import.meta.glob('../../**/*.{ts,tsx}', {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>

// The API client is client.ts, which re-exports core.ts (where startRun lives).
const API_CLIENT = /api\/(client|core)\.ts$/

const app = Object.entries(sources).filter(([path]) => !/__tests__|\/test\//.test(path))

describe('2.0 has no path that turns AI on', () => {
  it('reads the application sources', () => {
    expect(app.length).toBeGreaterThan(20)
  })

  it('never calls startRun with an argument', () => {
    for (const [path, text] of app) {
      if (API_CLIENT.test(path)) continue
      expect(`${path}: ${/\bstartRun\([^)]/.test(text)}`).toBe(`${path}: false`)
    }
  })

  it('never sends use_ai outside the API client, and never calls /ai/', () => {
    for (const [path, text] of app) {
      if (API_CLIENT.test(path)) continue
      expect(`${path}: ${/use_ai\s*:/.test(text)}`).toBe(`${path}: false`)
      expect(`${path}: ${/['"`]\/ai\//.test(text)}`).toBe(`${path}: false`)
    }
  })
})
