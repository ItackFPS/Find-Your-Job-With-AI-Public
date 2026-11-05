# 0018 — Personal ranking is a bounded Bayesian model with a profile prior, learned from the journals without labels

Status: accepted (2026-11-05). Not implemented yet (stage L3 in shadow, L4 eligible). Signal table,
formulas and tests: `docs/plans/2026-11-05-learning-architecture-decisions.md` §4.3 (AD-3), with the
v1 proposal's AD-3 for the model structure.

## Context
Today's personalization (`src/personalization/engine.py`) averages tokens of liked offers and is
off by default; ADR 0009 says explicit feedback does not change rankings by default. The owner wants
automatic personal learning with no minimum label count and no learning settings (objective O-5),
while one early click must not take over the list and cold start must stay today's ranking.

## Decision
- **Model.** A per-install Bayesian linear preference model at the ranking stage (after semantic
  matching, where personalization runs today):
  - embedding part: ridge regression on the stored embedding with the **profile embedding as prior
    mean**;
  - occupation-group and structured features (distance, remote kind, contract, seniority, salary
    bucket, source, weak flags): per-value shrunk mean adjustments;
  - the adjustment is clipped to an **evidence-scaled cap** `cap(n)` that grows with the number of
    offers carrying a signal (no minimum, no threshold), then `cap_personalization_at_gates()` is
    re-applied (ADR 0002/0003). With no signal the adjustment is exactly 0 (INV-7).
- **Signals** (AD-3.1), rebuilt on the exposure levels of ADR 0016: rendered and visible are not
  evidence; *opened* is a weak positive once per offer; saved, apply link, applied and interview
  outcomes are increasing positives; employer rejection is not the user's preference; explicit
  stances count, and a **reason routes the signal only to that reason's feature** ("trop loin" moves
  distance, not the job's occupation); "unsure" clears a stance; Hide without a reason is 0 (ADR
  0017); calibration labels count from the tuning split only, never held-out. *Ignored* is logged
  but **inert at launch** (owner decision).
- **Aggregation per offer** is deterministic (precedence, clamp, recency half-life on effective time,
  profile-change discount) and reads each state family in the order defined by ADR 0016.
- **No position reweighting at launch.** The v1 `1/p(k)` was a heuristic, not inverse propensity
  scoring, and is removed. An "estimated position correction" from randomized exploration is a
  later, separate release-gated change (stage L9), claimed unbiased only under its stated model.
- **Updates** are recomputed from the journals at each run and, debounced, after each durable event.
  `personal_model_state` stores the posterior and a hash of its inputs, so it can always be rebuilt
  (INV-4); `personal_model_version` = algorithm version + config hash + state hash.
- **Exploration** ("À découvrir", 2 of 20 slots) is drawn from a pool built independently of the
  production and challenger rankings (ADR on shadow evaluation); a **diversity guard** limits one
  occupation group or company in the top 20.
- **Transparency** ("Ce que l'application a appris"): each learned effect in words with the offers
  that caused it, a per-offer explanation, Pause and Reset; no sliders, no numbers. While the model
  is in shadow the panel shows a neutral status and **no counter and no request for labels**.
- **Reset.** `job_scores.personal_*` written before the latest reset stay as history only: no
  ranking, shadow, health statistic or exploration draw reads them (readers filter on run start
  after the marker and on the current model version).
- **Two browser profiles** on one install: conflicting actions resolve by server insert order, a
  documented limit (owner decision, 2026-11-05; ADR 0016).

Every weight, cap, half-life, prior strength and slot count is an initial value shipped in
`config/personalization.yaml`; none is approved until measured.

## Consequences
- **ADR 0009 is reversed for ranking**: feedback changes the ranking by default once the shadow
  evaluation allows it. Feedback still never changes acceptance (INV-1) or the stored score (INV-3).
- The ranking changes only after the shadow evaluation and activation rule (next ADR) allow it
  (INV-6).
- Schema: `personal_model_state` (posterior, hash, paused, reset marker); `job_scores` gains
  `personal_adjustment`, `personal_model_version`, `lane` via `ensure_columns`.
- The legacy `job_feedback` table and `personalization_bridge` are retired: readable for one release,
  then dropped by a named data migration. `src/personalization/` keeps its cache, explanation and
  recency scaffolding.
- Tests: INV-7 bit identity at n = 0; reset gives a ranking bit-identical to a copy with pre-reset
  `personal_*` set to NULL; a reason-routed negative moves only its feature.
