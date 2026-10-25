import i18n from '../i18n'

// Canonical rejection-reason identifiers -> human-readable display labels
// (translations under rejectionReasons.<code>).
//
// The canonical codes come straight from the backend and must stay
// machine-readable there — this table is presentation-only, used solely to
// render the Stats page's "Why jobs get rejected" list. Codes mirror:
//   - src/pipeline/filter.py's _FILTER_CHECKS reason codes (location, remote,
//     work_mode, seniority, published_age, salary, title_keywords)
//   - src/requirements/models.py's HardRejectionReason.type values
//     (education, experience, principal_skill, mandatory_skill,
//     mandatory_secondary_skill)
const REJECTION_REASON_CODES = new Set([
  'location', 'remote', 'work_mode', 'seniority', 'published_age', 'salary', 'title_keywords',
  'education', 'experience', 'principal_skill', 'mandatory_skill', 'mandatory_secondary_skill',
])

/**
 * Human-readable label for a canonical rejection-reason code (e.g.
 * "title_keywords" -> "Title keywords"). Falls back to a generic title-cased
 * rendering of the raw code for anything not in the table above, so a new
 * backend reason code never displays as a raw, underscored identifier.
 */
export function rejectionReasonLabel(detail: string): string {
  if (REJECTION_REASON_CODES.has(detail)) return i18n.t(`rejectionReasons.${detail}`)
  return detail
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
}
