# Stage L4: personal ranking eligible (plan)

Date: 2026-11-05. Decisions:

- ADR 0019 (shadow evaluation and health monitor);
- ADR 0018 (personal preference model);
- ADR 0016 (surfaces);
- ADR 0015 (reset and data classes).

Detail: `2026-11-05-learning-architecture-decisions.md` §4.3 (AD-3.3–3.6) and §4.5 (AD-5).

## Starting point: the L3 simulator verdict

The L3 simulator (`docs/reports/2026-11-05-l3-simulator.md`) found that the v5 activation rule
misses its own criteria on the synthetic grid:

- false activation reaches 21.6 % (SR-1, high activity);
- true activation within 90 days is about 0 %.

No G3b values were adopted, and enabling activation is an owner decision.

L4 therefore ships every L4 mechanism with **activation behind a release flag that is off**
(`activation_enabled: false`, in addition to `experimental_enabled: false`). With the flag off:

- the rule is still evaluated and recorded (`personal_health.decision` shows what it *would*
  decide);
- the stored state never leaves SHADOW;
- the user always sees the production ranking.

This is the conservative reading of ADR 0019 ("production stays the default until a challenger has
sufficient positive evidence"). It also matches the stage's exit criterion: activation itself is
not an exit criterion.

## Exit criteria (v5 §6)

1. **Fault injection pauses.** A forced guardrail failure, or a contradicting sequence, on a
   personal health record in ACTIVE (set up in a test with the flag on) halves and then pauses.
   A guardrail failure pauses at once. The pause shows a notice with resume and reset.
2. **Activation rule live, release-gated.** Each run writes `personal_health`. With the flag off, the
   state is SHADOW whatever the evidence. With the flag on (tests only), ACTIVE applies the
   challenger ranking with `cap(n) × cap_factor`.
3. **INV-6 / INV-7 kept.** With the flag off, the run's visible ranking and stored scores are
   byte-identical to L3.
4. **Reset.** Reset clears `shadow_positive` and `personal_health` (derived), and the next run
   starts from SHADOW.

## Not in L4

- No change to the G3 threshold, and no sequential test: both are ADR 0019 changes awaiting the
  owner.
- No EXPERIMENTAL enabling (owner item 14).
- No position reweighting (L9).
- No Home redesign (P6).

## Slices (one PR each, sequential)

### L4.1 Live counting and health record: `src/learning/health.py`

Tables (both derived; reset clears them):

- `shadow_positive`: one row per offer, written once. Columns:
  - job_id, effective_at;
  - anchor run, anchor rule;
  - production and challenger positions;
  - stratum and session;
  - state at the time.
- `personal_health` (id = 1). Columns:
  - state, cap_factor, halvings;
  - decision with gates, p_pooled and per-stratum tallies;
  - flags;
  - model_version, updated_at.

Surface mapping from the app's `surface` / `sort_key` to S-codes:

- `jobs` with a search query → S2, with its sort key;
- `jobs` without a query → S1;
- `job_detail` → the surface of the list it was opened from, else S0;
- `decouverte` → S4.

The mapping is unit-tested.

Counting after the shadow block of each run:

- Positives come from the L3.1 signals with y ≥ +1.0.
- The anchor follows rules 1–3: exposures from `exposure_log`, each mapped to the latest run started
  at or before it; runs from `ranking_shadow`.
- Strata come from the surfaces seen in the 14 days before the positive.
- Then `gates.evidence`, `gates.decide` and `gates.monitor`.

Release flags live in the `personal_model:` section of `config/personalization.yaml`:
`activation_enabled: false` and `experimental_enabled: false`.

The step is skipped while learning is paused, and fails open.

Tests:

- anchoring and strata from exposures;
- once-per-offer;
- the flag off keeps SHADOW with a passing decision recorded;
- the flag on reaches ACTIVE;
- monitor halving and pause from a fixture;
- reset clears.

### L4.2 Personal ranking when ACTIVE: orchestrator

- When `personal_health.state == active` (reachable only with the flag on), the visible ranking is
  the challenger ranking, with the adjustment scaled by `cap_factor` and gates re-applied.
- When paused, or with the flag off, production is used.
- `job_scores.final_score` is unchanged (the deterministic score and the stored production score
  stay contracts). The visible order is recorded per run in a `visible_ranking` column or in stats.
- Tests:
  - the flag off gives byte-identical output (INV-6);
  - the flag on with ACTIVE reorders within `cap(n)`;
  - a paused state uses production;
  - fault injection: a guardrail failure during a run pauses, and the next run uses production.

### L4.3 Découverte (S4)

- The draw happens before ranking, from the eligible pool:
  - valid, passes every STRONG constraint, not hidden, not in S4 in the last 14 days;
  - no score, rank or floor.
- It is uniform with a recorded seed, |E| and π, and fills at most 2 slots.
- Drawn offers are removed from the S1 candidates.
- `ranking_shadow.s4_*` filled.
- The API returns the slots. The Jobs page shows a "Découverte" section in fixed positions, in
  random order within it, logging `surface = "decouverte"` with `randomized = true`.
- Tests:
  - the draw is independent of scores (property: permuting scores leaves the draw unchanged);
  - exclusions;
  - π recorded;
  - no S1 duplicate;
  - frontend section and exposure surface.

### L4.4 Transparency panel, pause notice, Quality section

- `GET /api/learning/status`: state, flags, counts, wording keys. While SHADOW, the neutral status
  ("L'application observe vos choix ; la personnalisation s'activera quand elle aura fait ses
  preuves"), with no counter and no request for labels.
- Pause notice with "Reprendre" (guarded; the monitor stays on) and "Réinitialiser" (the existing
  AD-12 reset).
- Quality page: the learning-curve counts (L3.5 route) and per-stratum counts, in the ADR 0019
  descriptive wording.
- Tests: API; panel states; notice actions; wording (never "meilleure", never "95 % de chances").

## Risks

- The `exposure_log` has no run id: anchoring maps an exposure to the latest run started before it.
  This is stated in `shadow_positive.anchor_rule`.
- The surface mapping depends on the frontend values. A new surface without a mapping is S0, which
  is excluded and never miscounted.
- Découverte shows poor matches by design (owner item 16). The volume is at most 2.
