# 0017 — Offer validity is operational; only strong user constraints reject; weak ones rank; Hide is list management

Status: accepted (2026-11-05). Not implemented yet (stage L5 for weak evidence; salary check after
the `matches_salary` correction). Classification table, routing and criteria:
`docs/plans/2026-11-05-learning-architecture-decisions.md` §4.2 (AD-2), with the v1 proposal's AD-2.

## Context
In the owner's blind labels, 5 of 7 relevant tuning offers and 3 of 4 relevant held-out offers were
rejected by checks whose evidence is weak (publication age, title words). A rejected offer never
reaches scoring, so nothing downstream can recover it. At the same time, an offer that is closed or
malformed is not a matter of taste, and showing it would mislead.

## Decision
- **Validity (AD-2a) is separate from preferences.** Closed or expired (ADR 0012, expiry), exact
  duplicate, malformed: decided in the existing validation, dedup and expiry paths, journaled with
  their own reason codes, never "strong/weak/unknown", excluded from learning, lanes and the
  systematic-filter-error detector (INV-1b). **Publication age is not validity**: an old offer may
  still be open.
- **User constraints (AD-2b) have three outcomes.** *STRONG → reject*, only when the constraint is an
  explicit user choice **and** the offer's value is known with high confidence (structured field or a
  rule measured ≥ 0.95 precision). *WEAK → keep, penalise, flag* (`weak_flags`, at most 0.10 each,
  0.25 total, before the ADR 0003 gates). *UNKNOWN → neutral*, never a rejection (as ADR 0004/0013).
  Title words, seniority words and age are never STRONG; only phrases the user typed as exclusions
  are.
- **No flooding.** Weak-flagged offers never enter the main list by default: those with final score
  ≥ 60 and occupation proximity ≥ 0.6 (interim `domain_relevance` ≥ 0.7) go to a capped, collapsed
  "En dehors de vos critères" lane that names the missed criterion and never notifies; the rest are
  stored as accepted with `lane = "below_threshold"` (never silently discarded). A weak-flagged offer
  enters the main list only through a positive personal adjustment, labelled as such.
- **Rollout through the lab.** `filters.weak_evidence_mode: reject | rank` defaults to `reject`, one
  check at a time (age, then title, then seniority), after a new dated pre-registration (the real-v3
  file #109 covers ranking candidates only) and human approval (ADR 0011).
- **Salary.** Compared only when offer and profile fall in the same comparability class (currency,
  gross/net basis, period, working time; ADR on the market plane). Everything else is UNKNOWN and
  raises no flag. Until the France Travail basis is verified and the profile salary contract exists,
  the check is UNKNOWN for nearly every offer, by design. **The `matches_salary` correction lands
  before** any salary filtering, the profile salary contract, or any collector mapping new salary
  values (owner priority).
- **Hide (AD-2c) is list management, not a judgement.** It removes that offer and its exact
  duplicates from every list and lane; near-duplicates get a "similaire à une offre masquée" note.
  It produces no generalising negative. An optional "Pourquoi ?" chip records an explicit reason,
  routed only to that feature. After ≥ 3 hides of one occupation group or company within 30 days the
  app *asks*; without an answer the pattern adds at most −0.2 per hidden offer on that structured
  feature, never on the embedding, under `cap(n)`. "Afficher à nouveau" undoes it.

All numbers above are initial values, not approved parameters.

## Consequences
- ADR 0001's wording becomes "strong-constraint filtering" (principle kept); ADR 0012's age becomes
  graded evidence while expiry still excludes; ADR 0003's domain gate basis moves to the occupation
  model when it lands.
- Additive schema: evidence items gain `strength`, exposure gains `lane` (decision `schema_version`
  +1), score breakdown gains `weak_flags` and `weak_penalty`. No new table.
- Criteria (pre-registered; tuning exploratory, held-out decisive): relevant offers reaching main or
  lane ≥ 2× baseline; main-list precision not lower beyond the paired CI; lane cap respected in 100 %
  of runs; 0 new acceptances of a STRONG violation on the synthetic self-check.
- Rollback: the per-check flag back to `reject`, or the lab rollback of the bundle.
