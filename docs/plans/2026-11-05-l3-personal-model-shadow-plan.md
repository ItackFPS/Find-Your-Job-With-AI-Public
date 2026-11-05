# Stage L3 — personal model in shadow, and the activation simulator (plan)

Date: 2026-11-05. Decisions: ADR 0018 (personal preference model), ADR 0019 (shadow evaluation and
health monitor), ADR 0016 (exposure levels, surfaces, per-offer ordering), ADR 0015 (reset, data
classes). Detail: `2026-11-05-learning-architecture-decisions.md` §4.3 (AD-3) and §4.5 (AD-5).

## Goal and exit criteria

The personal model is **computed and journaled at every run, never shown**. The production ranking,
the stored deterministic score and every list the user sees stay exactly as they are (INV-3, INV-6).
The AD-5 activation rule exists as pure functions and is exercised by a simulator; it is not wired
to any live state yet (that is L4).

Exit criteria (v5 §6):

1. **INV-7 bit identity**: with no signal, the personal adjustment is exactly 0 for every offer, and
   the run's ranking and stored scores are byte-identical to a run without the L3 code path.
2. **Reset**: after Reset learning, a run gives a challenger ranking bit-identical to a run on a copy
   whose pre-reset `personal_*` values and pre-reset signals are removed.
3. **Offline learning curve**: an evaluation tool replays the journal in time order and reports,
   for each positive, where the challenger trained only on earlier signals would have placed it
   against production. Reported on synthetic data in tests; on real data during the fresh-copy run.
4. **Simulator**: false activation ≤ 5 % of 180-day histories at true win rate 0.5 (low, typical,
   high activity), true activation ≥ 80 % within 90 days at 0.70, adversarial halving then pause,
   SR-1 … SR-5 meeting their criteria with EXPERIMENTAL disabled as shipped (SR-4/SR-5 are also run
   with it enabled, to record whether a release may enable it). The simulator sets n_LB,min,
   c_contra, c_het and m; their values and the grid are written to the simulator report.

## Not in L3

- No visible change: no Découverte (S4) slots, no transparency panel, no activation, no health
  pause (L4). `ranking_shadow` has nullable S4 columns, filled from L4.
- `shadow_positive` and `personal_health` are created in L4, where the live counting runs; L3
  implements and tests the counting rules as pure functions that L4 will call.
- The legacy `job_feedback` engine keeps running unchanged; its retirement is a named migration later.
- Ignored stays inert (weight 0, ADR 0018). No position reweighting (L9).

## Slices (one PR each, sequential)

### L3.1 Signal aggregation — `src/learning/signals.py`, `src/learning/config.py`

- Reads `feedback_events` (not superseded, at or after `learning_reset_at`), resolves the current
  state per offer and family in insert order (ADR 0016), and returns one `OfferSignal` per offer:
  aggregated value y (precedence and clamp), the per-feature routing (all features / embedding +
  occupation / one reason's feature only), the effective time and the recency factor.
- Weights per AD-3.1: opened +0.25 once; saved / to_apply +1.0; apply link +1.2; applied +1.5;
  interview, offer, accepted +2.0; employer rejected 0 (earlier positive kept); withdrawn cancels
  the outcome positive; interested +1.2; not interested −1.2 (with a reason: that reason's feature
  only); unsure clears the stance; hidden 0, hidden + reason −1.2 to that feature.
- Calibration labels are **not read in L3**: they live in evaluation datasets outside the journal,
  and production does not import the evaluation package. Their tuning-split use (held-out never
  read) is added with L4, through a label reader that applies `is_heldout`.
- Config: a `personal_model:` section in `config/personalization.yaml` (weights, half-life, clamp,
  prior strength, cap curve), every value marked unapproved.
- Tests: each row of the signal table; reason routing; reset filter; superseded events ignored;
  insert-order state resolution.

### L3.2 Model and shadow journal — `src/learning/model.py`, `src/learning/shadow.py`

- Bayesian linear model: ridge on the stored job embedding (`job_embeddings`, same model name as the
  semantic stage) with the profile embedding as prior mean; per-value shrunk mean adjustments for
  the structured features (remote kind, contract, seniority, salary bucket, source, distance band);
  output clipped to `cap(n)` (grows with the number of offers carrying a signal, no threshold), then
  `cap_personalization_at_gates()` re-applied. n = 0 → exactly 0.
- `personal_model_state` (derived): posterior summary, input hash, algorithm version, config hash.
  Rebuilt from the journal at each run (INV-4); `personal_model_version` = algorithm + config hash
  + state hash.
- Pipeline: after the production ranking is final, compute the challenger ranking **in shadow**.
  `job_scores` gains `personal_adjustment` and `personal_model_version` (`ensure_columns`);
  `ranking_shadow(run_id, job_id, production_position, challenger_position, s4_seed, s4_pool_size,
  s4_pi)` stores both positions (positions beyond 100 stored as computed; readers clamp to 101).
  The production list, `final_score` and the deterministic score are untouched. Fail-open: any
  error is logged, the run keeps its result. Skipped while learning is paused.
- Data classes: `personal_model_state`, `ranking_shadow` → derived; reset clears them.
- Tests: INV-7 bit identity on a fixture run; a positive on an offer raises similar offers and not
  dissimilar ones; reason-routed negative moves only its feature; cap(n) bound; the reset test of
  exit criterion 2; failure in the shadow path leaves the run completed.

### L3.3 Activation rule as pure functions — `src/learning/gates.py`

- Positive anchoring (one positive per offer, anchor run rule 1–3, 14-day window), discordance
  (≥ 3 positions, beyond 100 → 101), clustering cap (3 per session), strata (LB / IM / PF, "any PF
  exposure → PF"), G1–G5, G3 exact Beta(1, 1) posterior, G3b (a) and (b) with an exact or Monte
  Carlo P(p_LB < p_PF − m), states SHADOW / EXPERIMENTAL / ACTIVE / paused, EXPERIMENTAL release
  flag off, health monitor (halve, pause on second halving within 30 days, top-20 rate drop,
  guardrail failure → immediate pause).
- Tests: the v5 G3 arithmetic table (9/12, 11/15, 14/20, 20/30) and the pause arithmetic (≤ 2 of 8,
  ≤ 3 of 10, ≤ 4 of 12); a vacuous G3b never passes; EXPERIMENTAL never reachable with the flag off;
  EXPERIMENTAL-period PF/IM positives excluded.

### L3.4 Simulator — `src/learning/simulator.py` (+ CLI) and its report

- Generates 180-day histories at low / typical / high activity over a grid of click-model
  parameters (position bias, session length, positives per session), feeds them through
  `gates.py`, and counts ACTIVE and EXPERIMENTAL activations separately.
- Scenarios: baseline false activation (0.5), true activation (0.70), adversarial, owner replay
  (when AD-6 logs exist; skipped with a note otherwise), SR-1 exposure-fit challenger, SR-2
  correlated outcomes ρ ∈ {0, 0.2, 0.5}, SR-3 stratum disagreement, SR-4 thin LB, SR-5 feedback
  loop (exclusion rule on, and a diagnostic run with it off).
- Seeded and deterministic. The CI test runs a small number of histories per scenario; the full run
  (≥ 1000 histories per cell) produces `docs/reports/2026-11-05-l3-simulator.md` with the chosen
  n_LB,min, c_contra, c_het, m and the worst grid point per criterion. If SR-2 at ρ = 0.5 fails,
  the clustering cap or G2 is revised (never G3) and the report says so.

### L3.5 Offline learning curve — `src/evaluation/learning_curve.py`

- Child-process, read-only evaluation tool (ADR 0007): replays the journal in time order; for each
  positive, trains the model on signals strictly earlier, and reports the challenger's and
  production's positions for that offer in its anchor run, with win counts and the descriptive
  wording of ADR 0019 (never "meilleure").
- `GET /api/quality/learning-curve` returns counts only. No UI in L3 (the Quality section comes with
  L4's panel).
- Tests: synthetic journal with a planted preference → challenger wins grow with signals; no signal
  → no rows; held-out labels never read; the tool opens the database read-only.

## Risks

- Embedding availability: offers without a stored vector get only the structured part. Measured as
  a coverage figure in `personal_model_state`.
- Run time: ridge on 384-dimensional vectors with at most a few thousand signals is milliseconds;
  scoring all ranked offers is one matrix product. Measured in `stage_times["shadow"]`.
- The simulator's grid is not the owner's behaviour; criteria are re-checked once AD-6 has real
  volumes (fresh-copy run, then L4).
