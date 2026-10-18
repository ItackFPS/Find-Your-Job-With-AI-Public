# Feedback is an append-only journal of facts, linked to the decision it reacts to

User signals were scattered and mostly unusable: `job_feedback` could only be written from a
CLI, `application_status` and `outcome_events` fed nothing, and none of it referenced what the
system had decided. `src/feedback/` adds one append-only table, `feedback_events`
(UPDATE/DELETE abort via triggers; a correction is a new event). Each event keeps its signal
class — **explicit** (what the user says: interested / not interested / mis-ranked with
structured reasons), **behavior** (viewed, apply link opened), **outcome** (application-status
transitions) — plus provenance (`source`), timestamp, free metadata, and the `decision_id` and
`strategy_version` of the latest decision in force, so "what did the system decide, under which
strategy, on what evidence, and what did the user do next?" is a join.

**No weight or reliability is stored at insertion.** How much an explicit click, a behavior
or an outcome is worth is an analysis-time decision (P4/P5), so the interpretation can be
revised without rewriting history.

## Consequences

- Legacy `jobs.application_status` values are never rewritten. A versioned mapping derives a
  canonical value (`to_apply, applied, interview, offer, accepted_offer, employer_rejected,
  withdrawn`) stored next to the legacy one. `accepted`, `rejected` and `denied` are
  **unmapped by default**: nothing defines them and the UI shows two error-coloured buttons for
  rejected/denied, so they are not assumed synonymous. The owner resolves them in
  `config/feedback.yaml`; `python -m src.feedback.cli inspect-statuses` reports the values and
  transitions actually present (read-only), and `backfill` imports historical transitions.
- Because events are immutable, the canonical status stored on an event is only a snapshot
  (with the `mapping_hash` it was made under); analysis derives it from `legacy_to` with the
  current mapping (`FeedbackService.canonical_for`), so resolving `denied` later also fixes
  history.
- Each outcome event carries the `outcome_event_id` it mirrors (unique-indexed): the live
  mirror and the backfill can never double-journal a transition. Provenance is real: the UI
  sends `source: "ui"`, any other API caller is recorded as `api`, imports as `import`.
- A client may pin the `decision_id` it displayed (validated against the job); otherwise the
  latest decision is used, and a decision-store failure records the feedback unlinked rather
  than losing it.
- Notes are free text kept locally and, by design, cannot be edited or deleted through the
  application (append-only triggers). Erasure is a deliberate owner-side database operation.
- Selection bias is real (only exposed jobs get feedback); the linked decision carries the
  exposure (rank/tier), and behaviour is kept separate from explicit feedback.
- Explicit feedback does **not** change rankings by default: mirroring it into the legacy
  personalization table is opt-in (`personalization_bridge`).
- Discord stays outbound-only; no gateway/worker is added to capture reactions.
