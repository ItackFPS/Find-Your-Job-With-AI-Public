# 0019 — Personal ranking activates only on positive shadow evidence and pauses itself more easily than it activates

Status: accepted (2026-11-05). Not implemented yet (simulator in stage L3, activation in L4). Rules,
arithmetic, schema and simulator scenarios:
`docs/design/learning-architecture-decisions.md` §4.5 (AD-5).

## Context
A personal model (ADR 0018) must not change what the user sees until it has shown it agrees with the
user's actual choices better than today's ranking (INV-6). Every outcome is observed while
production decides what is visible, so a shadow comparison is biased both against the challenger
(exposure selection) and for it (self-reinforcement: it learns what production made clickable). v1's
"activate after 30 days with no evidence against" would have activated on 1 win of 1.

## Decision
- **Production stays the default** until a challenger has sufficient **positive** evidence; no
  deadline, no time-based fallback. This applies to the first enabling and to every later personal
  algorithm or config change from a release.
- **Evidence unit.** A positive is an offer reaching y ≥ +1.0 within 14 days (opened alone is not a
  positive), counted once per offer and anchored to exactly one run by a deterministic rule recorded
  once in `shadow_positive` (latest visible exposure before the positive's effective time, else the
  latest run that ranked it; otherwise not counted). It is judged against the two rankings stored for
  that run (`ranking_shadow`), computed only from signals received before it. A discordant positive
  is placed ≥ 3 positions apart; at most 3 count per session.
- **Surfaces and strata.** Every event carries its surface (ADR 0016): S1 main list, S2 search, S3
  rule lanes, S4 randomized exploration, S5 external entry, S0 unknown (excluded). Strata:
  **LB** (less biased: S4 and S2 sorted by date or distance), **IM** (S3: reported, never evidence),
  **PF** (production-favouring: S1, S5, S2 by relevance). A positive is LB only if every surface it
  was seen on in the 14 days before is LB; any PF exposure makes it PF.
- **Exploration pool S4** ("Découverte", ≤ 2 slots) is drawn **before** the main list is ranked,
  uniformly (or stratified by occupation group) from offers that are valid, pass every STRONG
  constraint, are not hidden and were not in S4 in the last 14 days: no production or challenger
  score, rank or floor. Seed, pool size and each inclusion probability π are recorded. **No score
  floor** (owner decision, 2026-11-05): some Découverte offers will be poor matches.
- **Activation gates, all required:** G1 exposure (≥ 14 days and ≥ 10 sessions with visible
  exposure); G2 evidence (≥ 20 positive offers and ≥ 12 discordant positives); G3 pooled posterior
  P(challenger win rate > 0.5) ≥ 0.95 under Beta(1, 1); G3b contradiction guard on Beta posteriors
  of the LB and PF win rates (block when LB contradicts, or when the challenger wins clearly more on
  PF than on LB — the signature of self-reinforcement); G4 guardrails green; G5 no pause condition.
- **States.** SHADOW (default, after a pause or reset) → ACTIVE only when G1–G5 hold **and** LB
  reaches its minimum volume and G3b passes. A vacuous G3b is never a pass. An intermediate
  EXPERIMENTAL state (small capped adjustment, labelled "Personnalisation expérimentale, pas encore
  confirmée", never recorded as validated) exists in the design but **ships disabled** (owner
  decision, 2026-11-05); it may be enabled by a release only after simulator scenarios SR-4 and SR-5
  pass. With it disabled, a user without enough LB evidence stays in SHADOW.
- **Health monitor, asymmetric.** Halve the cap at ≥ 8 discordant positives with P(worse) ≥ 0.80, or
  when the 14-day top-20 positive rate falls > 50 % below its trailing median; pause on the second
  halving within 30 days, and immediately on any guardrail failure. Resuming automatically needs a
  fresh shadow window meeting the full rule; a manual "Reprendre" stays guarded.
- **Wording.** The posterior is a descriptive measure of agreement with observed choices under an
  independence assumption, not causal proof: "Sur N offres que vous avez retenues, la version
  personnalisée les aurait classées plus haut dans X cas." Never "meilleure" or "95 % de chances".
- **Accepted consequence** (owner decision): a low-activity user may never get ranking
  personalization; profile suggestions they confirm and Hide still work. Causal evaluation
  (interleaving) is not done now; revisit after L4.

All counts, windows and thresholds (and n_LB,min, c_contra, c_het, m, the EXPERIMENTAL cap and
timeout) are initial values; G3b's and EXPERIMENTAL's are set by the simulator.

## Consequences
- **Before L4 ships**, a simulator must show: false activation ≤ 5 % of 180-day histories at true
  win rate 0.5 across activity levels; true activation ≥ 80 % within 90 days at 0.70; adversarial
  sequences halve then pause; the owner's replay causes no pause; and self-reinforcement scenarios
  SR-1 to SR-5 (exposure-fit challenger, correlated outcomes ρ ∈ {0, 0.2, 0.5}, stratum
  disagreement, thin LB, feedback loop) meet their criteria. If correlated outcomes fail, the
  clustering cap or G2 is revised through the simulator, never G3.
- Schema: `ranking_shadow` (with S4 seed, pool size, π), `personal_health` (posterior, sessions,
  positives, `activation_state`, per-stratum counts), `shadow_positive` (one row per offer, written
  once). All are derived personal data under ADR 0015.
- The Quality page reports per-surface and pooled results, the LB ∪ IM sensitivity line and an
  attribution warning when the two attribution rules disagree.
