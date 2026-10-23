// Links built from collected data (a job's apply URL) or settings (a Discord invite) are rendered
// only when they are absolute http(s) URLs. React blocks `javascript:` hrefs, but not `data:`,
// `vbscript:`, `file:` or a custom app protocol a job board could put there.
export function httpUrl(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const url = new URL(value.trim())
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null
  } catch {
    return null
  }
}
