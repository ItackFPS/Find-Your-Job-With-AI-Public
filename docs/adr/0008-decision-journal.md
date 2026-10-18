# Every job gets an auditable, versioned decision record (observability only, fail-open)

The pipeline used to forget its decisions: accepted jobs had no trace, rejected jobs kept
only a code and a few metadata fields (no text), gate evidence and penalties were discarded
after the run, and explanations were recomputed from separate heuristics. The decision
journal (`src/decisions/`, tables `job_decisions`, `job_snapshots`, `strategy_versions` in
`jobs.db`) persists one `DecisionRecord` per (run, job) for accepted **and** rejected jobs:
outcome, deciding stage, `strategy_version`, deterministic score, typed three-valued
`EvidenceItem`s, conflicts, stage trace and exposure (rank/tier).

We chose a separate recorder fed by read-only hooks in `run_pipeline` (rather than changing
the stages) so that nothing about a decision, score or ranking can change:
`tests/test_decision_journal_orchestrator.py` runs the real pipeline with the journal on and
off and requires identical results, and a journal failure is swallowed (fail-open).

## Consequences

- Rejected jobs keep a content-addressed, compressed snapshot of their text (configurable,
  retention-bounded; text can be purged while the audit metadata stays) — the only way to
  audit false negatives later, since `jobs` never stored rejected jobs.
- Job text follows one policy everywhere: `store_rejected_text` / `store_accepted_text` govern
  both the snapshots and the quoted evidence spans (`span.text` is blanked when text is not
  kept); retention removes snapshot text and old span quotes (with `secure_delete` and a WAL
  truncate) while decisions and offsets stay. Re-seeing identical content refreshes the clock
  (and restores purged text if the job is rejected again).
- The pipeline is the source of truth: a job that reached scoring is `accepted` even if a
  stage observer had recorded a rejection the pipeline then did not apply (e.g. gate error
  fallback); the disagreement is stored as a conflict. A failure on one job skips that job's
  observation, never the run's journal.
- `strategy_version` (hash of config files + rule/prompt versions + run mode) ties every
  decision, and later every piece of feedback, to the strategy that produced it.
- `decision_confidence` is reserved and never a verbalised LLM confidence; the eligibility
  gate's self-reported confidence is stored as a signal only.
- Evidence labelling is conservative — the journal never claims more than was established: a
  hard-filter check that could not reject anything (no constraint configured) yields no
  evidence; a configured check on a job lacking its data is **UNKNOWN** (the filter accepts by
  default); only a configured check with data present is SATISFIED. A gate dimension is
  SATISFIED only when the gate evaluated it and applied no penalty (soft penalties are recorded
  as negative evidence; skills are UNKNOWN when no skill gate ran). Evidence for passing jobs is
  derived from the verdict that already happened — the checks are never re-run. Work-mode
  contradictions between metadata and description are recorded as conflicts (only against a real
  metadata claim: `Job.remote=False` means "unknown").
- `observed`/`expected` are structured metadata (job fields, config slices); description-derived
  quotes (evidence spans, conflict quotes) follow `store_*_text`. Scoring evidence (per-factor
  values, gates, penalties) arrives in a later slice.
- `GET /api/jobs/{id}/explanation` renders the explanation from the journaled decision of the same
  run (`explain.explain_from_decision`: no heuristics of its own), so the explanation cannot
  disagree with the decision; jobs that predate the journal (or older runs / journal off) keep the
  legacy `generate_explanation` path, flagged by `explanation_source`.
- Volume grows with every run; mitigated by content-addressed snapshots and retention.
