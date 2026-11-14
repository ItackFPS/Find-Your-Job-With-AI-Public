# 0030 — The personal order applies under the user's control, bounded and journaled

Status: accepted (2026-11-09, owner brief "Product reality review"). Supersedes ADR 0019 on one
point: the statistical activation rule no longer decides whether a personal order is shown. Its
counting, shadow comparison and health monitor stay.

## Context
ADR 0019 made the personal order wait for positive shadow evidence. The simulator
(`docs/reports/2026-11-05-l3-simulator.md`) and the correction work that followed showed that one
person's signal volume cannot satisfy that rule: years of evidence for low power. The result in the
product was that nothing a person did reached what they saw. On the installed 2.0.0rc1, signals were
journaled, the personal model had never run because learning was paused, and the legacy
personalization received nothing because its bridge from the journal is off. The owner asked for a
real, measurable and controllable integration.

## Decision
- The order the user sees follows their actions once there are at least three **strong** signals
  (saved, applied, interested, dismissed). Opening an offer is a weak signal and never counts towards
  this. Pausing learning turns it off at once.
- The effect is bounded by the evidence-scaled cap of ADR 0018, and the scoring gates are re-applied,
  so a confirmed mismatch cannot rise. Stored scores never change (INV-3).
- Both orders are journaled per run in `ranking_shadow`. That is the before/after comparison, shown
  in Settings and available per offer.
- The model also learns the words of titles and skills, so a preference can be named. Every learned
  preference can be forgotten and restored; a reset clears the corrections with the rest.
- The source of an offer is no longer a learned feature: it only reflected where the user looked.

## Consequences
The guarantee of ADR 0019, that a personal order is shown only once shown better, is given up in
exchange for a bounded effect that the user can see, correct and turn off. Whether the order improves
with everyday signal volumes is not established: cross-validated on 67 human labels
(`python -m src.evaluation.learning_lift`), AUC went from 0.607 to 0.631 and precision@5 stayed at 0.2,
which is within noise with 7 accepted offers. The measure must be repeated as labels accumulate.
