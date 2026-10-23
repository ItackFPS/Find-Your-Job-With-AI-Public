/**
 * The app mark, drawn like packaging/windows/FindYourJob.ico and public/favicon.svg: a
 * magnifying glass over a briefcase on a rounded blue square. Brand colours are fixed, not
 * themed, so the mark looks the same everywhere it appears.
 */
export function AppLogo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" aria-hidden="true" focusable="false">
      <rect x="2" y="2" width="96" height="96" rx="20" fill="#2563eb" />
      <circle cx="44" cy="42" r="23.5" fill="none" stroke="#fff" strokeWidth="7" />
      <path d="M61.7 59.7 79.4 77.4" stroke="#fff" strokeWidth="11" />
      <rect x="31" y="39" width="26" height="14" rx="3" fill="#fbbf24" />
      <rect x="40.2" y="34.2" width="7.6" height="6" rx="1.6" fill="none" stroke="#fbbf24" strokeWidth="2.5" />
    </svg>
  )
}
