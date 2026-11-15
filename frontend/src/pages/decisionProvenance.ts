// Where an experience, education or salary requirement came from (Phase B B6-C): a structured field
// of the source (France Travail's experienceExige, its salary field…), text read in the offer, or not
// stated. Other dimensions have no such provenance and show nothing.
import type { DecisionEvidence } from '../api/client'

export type Provenance = 'structured' | 'text' | 'unknown'

const DIMENSIONS = new Set(['experience', 'education', 'salary'])
// evidence_source values a structured source field produces (src/requirements/models.py EvidenceSource).
const STRUCTURED = new Set(['metadata', 'structured', 'structured_field'])
const FROM_TEXT = new Set(['heuristic', 'llm_corroborated', 'llm_uncorroborated', 'description'])

export function provenanceOf(e: DecisionEvidence): Provenance | null {
  if (!DIMENSIONS.has(e.dimension)) return null
  if (e.state === 'unknown') return 'unknown'
  const source = e.source?.evidence_source ?? ''
  if (STRUCTURED.has(source)) return 'structured'
  if (FROM_TEXT.has(source) || (e.span && e.span.text)) return 'text'
  return 'unknown'
}
