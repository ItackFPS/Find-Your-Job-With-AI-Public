// Canonical rejection-reason identifiers -> human-readable display labels.
//
// The canonical codes come straight from the backend and must stay
// machine-readable there — this table is presentation-only, used solely to
// render the Stats page's "Why jobs get rejected" list. Codes mirror:
//   - src/pipeline/filter.py's _FILTER_CHECKS reason codes (location, remote,
//     work_mode, seniority, published_age, salary, title_keywords)
//   - src/requirements/models.py's HardRejectionReason.type values
//     (education, experience, principal_skill, mandatory_skill,
//     mandatory_secondary_skill)
const REJECTION_REASON_LABELS: Record<string, string> = {
  location: 'Location',
  remote: 'Remote',
  work_mode: 'Work mode',
  seniority: 'Seniority',
  published_age: 'Published age',
  salary: 'Salary',
  title_keywords: 'Title keywords',
  education: 'Education',
  experience: 'Experience',
  principal_skill: 'Principal skill',
  mandatory_skill: 'Mandatory skill',
  mandatory_secondary_skill: 'Mandatory secondary skill',
}

/**
 * Human-readable label for a canonical rejection-reason code (e.g.
 * "title_keywords" -> "Title keywords"). Falls back to a generic title-cased
 * rendering of the raw code for anything not in the table above, so a new
 * backend reason code never displays as a raw, underscored identifier.
 */
export function rejectionReasonLabel(detail: string): string {
  const known = REJECTION_REASON_LABELS[detail]
  if (known) return known
  return detail
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
}
