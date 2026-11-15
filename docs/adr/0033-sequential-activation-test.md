# 0033 — The activation rule's G3 is an anytime-valid sequential test

Status: accepted (2026-11-15, Phase B B9-B, owner decision D2). Amends ADR 0019's G3 and the G3b
parameters.

## Context
ADR 0019 requires, before activation can be enabled, false activation ≤ 5 % of 180-day histories at a
true win rate of 0.5. Its G3 was a fixed posterior, P(challenger win rate > ½) ≥ 0.95. The rule is
evaluated after every session, so that threshold is tested again and again: the L3 simulator
(`docs/reports/2026-11-05-l3-simulator.md`) measured up to 21.6 % false activation, and noted that only
"a sequential test (an error budget spent across looks), or keeping activation off" remained.

Owner decision D2 (2026-11-14): personalised ranking activates automatically, without user approval,
once predefined, statistically validated thresholds are consistently met; it only reorders eligible
offers; it rolls back automatically when quality deteriorates. Thresholds are defined in the evaluation
strategy, not invented.

## Decision
- **G3** passes when the e-value of the pooled discordant positives reaches `1 / α`, with
  α = 0.05 (`src/learning/gates.py`, `e_value`, `GateParams.g3_rule = "e_process"`). The e-value is
  the likelihood ratio of a uniform mixture over win rates in (½, 1] against ½. With independent
  outcomes and a challenger that is not better, it is a nonnegative supermartingale starting at 1, so
  the probability that it **ever** reaches 1/α is at most α (Ville's inequality), however often the
  rule is evaluated. α is ADR 0019's own criterion, not a tuned value.
- **G3b parameters** are re-chosen by the simulator's search with the new G3 (`choose_params`; grid
  widened to c_contra 0.1, c_het 0.3, n_LB,min 15): n_LB,min = 10, c_contra = 0.10, c_het = 0.30,
  m = 0 (were 6, 0.80, 0.80, 0.10). The guard's form is unchanged.
- G1, G2, G4, G5, the per-session cap, the states and the health monitor are unchanged.
- The former posterior rule is kept only as the simulator's comparison (`g3_rule = "posterior"`).
- ADR 0019's wording rule stands: the result is described as agreement with observed choices, never as
  "better" or as a probability of being better.

## Consequences
- Correlated outcomes and self-reinforcement (SR-1, SR-2) are not covered by Ville's guarantee; the
  simulator measures them, and the per-session cap, G2 and G3b remain the levers for them (ADR 0019).
- The rule needs more evidence than before (for example 16 wins of 20 discordant positives, 36 of
  50). A low-activity user may never get ranking personalization (ADR 0019's accepted consequence).
- **Safety is bought with power.** In a per-combination dump of the search grid (150 synthetic
  histories per cell, n_LB,min 10 or 15, c_contra 0.1 or 0.2, c_het 0.3 / 0.5 / 0.8, m 0 or 0.1,
  cap 1 or 3), only the combinations with c_het = 0.3 and m = 0 kept every false-activation cell at
  or below 5 %. With c_het = 0.5 or 0.8 the worst cell (SR-1 β = 2, high activity) reached 6.7 % to
  24 %. With c_het = 0.3 the guard blocks unless the Découverte (LB) stratum agrees with the rest at
  least as clearly, and because LB has fewer observations it also blocks some challengers that are
  strong on both strata. True activation (win rate 0.70, typical activity) is then 0 % to 1.8 %
  within 90 days across the click models, and 11.9 % on average within 180 days (1,000-history report):
  ADR 0019's 80 %-within-90-days power criterion is not met. An LB-only variant (the e-process on LB alone, no c_het) was also tried: it met the
  5 % bound and activated no more often.
- Activation is enabled as D2 decides (`personal_model.activation.activation_enabled: true`), but on
  these synthetic volumes it will rarely trigger; production ranking is unchanged until it does. The
  parameters are re-derived once real AD-6 volumes exist; a guard that compares LB with a fixed bar
  rather than with PF's posterior is the first candidate to recover power.
- Simulator results: `docs/reports/2026-11-15-sequential-activation.md`.
