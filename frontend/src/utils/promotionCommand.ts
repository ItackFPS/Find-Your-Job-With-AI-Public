import type { LabExperiment } from '../api/client'

// Lab-derived identifiers end up in a command the user pastes into a terminal (ADR 0011: lab
// output is untrusted data): only plain identifiers are ever put there, anything else falls back
// to placeholders the user fills in.
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/

export interface PromotionPlaceholders { experiment: string; dataset: string; name: string; reason: string }

/** The human-only CLI promotion command (owner decision I3: promotion is never a UI action). */
export function promotionCommand(exp: Pick<LabExperiment, 'experiment_id' | 'dataset_id'> | null,
  placeholders: PromotionPlaceholders): string {
  const safe = exp !== null && SAFE_ID.test(exp.experiment_id) && SAFE_ID.test(exp.dataset_id)
  const id = safe ? exp.experiment_id : `<${placeholders.experiment}>`
  const dataset = safe ? `data/benchmarks/${exp.dataset_id}` : `<${placeholders.dataset}>`
  return `python -m src.lab approve ${id} --dataset ${dataset} --by "<${placeholders.name}>" --reason "<${placeholders.reason}>"`
}
