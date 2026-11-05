# Architectural decision proposal: a learning application

Status: **superseded in part by `2026-11-05-learning-architecture-decisions.md` (v5, approved
2026-11-05).** Kept because v5 refers to it for the sections it does not repeat. Where the two
disagree, v5 wins.
Base: public repository `main` @ `6207a7c`. It follows the continuous-learning audit of 2026-11-05
(owner direction: agreement with D1–D10, plus objectives O-1 to O-5 below).

Each decision has an id (`AD-n`). The owner approves, amends or rejects each one on its own
(§12). Approved decisions become numbered ADRs in `docs/adr/` (0014 onwards), each in its own PR,
before any implementation PR that depends on it.

---

## 0. Owner objectives this proposal must satisfy

| # | Objective | Where it is addressed |
|---|---|---|
| O-1 | Professional potential: transferable skills, related occupations, experience and education, not exact keywords | AD-7 (occupation model, reachability), AD-2 (no keyword rejection) |
| O-2 | Market intelligence built from every collected offer | AD-8 |
| O-3 | Self-monitoring: sources, extraction, duplicates, systematic filter errors, ranking regressions | AD-9, AD-5 |
| O-4 | Works for every occupation and profile; France first, expansion possible | AD-7 (ROME + ESCO, scheme-agnostic tables), AD-1 (knowledge packs) |
| O-5 | Automatic personal learning: no minimum labels, no learning configuration | AD-3, AD-4, AD-5, AD-6 |

Constraints kept from the owner: local-first; no server; no expensive AI dependency; no new
infrastructure without demonstrated value; measurable; never silently degrade production.

---

## 1. Invariants (true for every decision below)

- **INV-1. Learning never overrides a strong constraint.** Personal, market and operational learning
  can reorder offers and propose changes. None of them can accept an offer that violates a strong
  constraint (AD-2), and none can reject an offer.
- **INV-2. Nothing personal leaves the install.** No network call carries profile data, signals,
  learned state or aggregates (AD-1, AD-12). This is enforced by a test.
- **INV-3. The stored score stays deterministic** (ADR 0002). Learned adjustments live only in
  per-run ranking (`job_scores`) and in their own derived tables.
- **INV-4. Learned state is derived and can be rebuilt.** Learned state is recomputed from journals
  (`feedback_events`, `exposure_log`, `jobs`). It is never the only copy of anything, so deleting it
  is always safe.
- **INV-5. Every learned effect is visible, explainable and undoable by the user,** in the UI (rule
  R-C2), without a terminal or JSON (rule R-C5).
- **INV-6. A new learned behaviour runs in shadow before it changes what the user sees, and it can
  revert itself automatically** (AD-5).
- **INV-7. Cold start is today's behaviour.** With no signals, the ranking is identical to the
  current deterministic + semantic ranking (bit-identical order; tested).
- **INV-8. The four planes stay separate** (§2). Data crosses planes only through the interfaces
  listed in §2.

---

## 2. Four planes and how they stay separate

| Plane | Question | Inputs | Owns (writes) | May influence | Must never |
|---|---|---|---|---|---|
| **P: Personal** | What does *this user* want? | profile, `feedback_events`, `exposure_log`, job features (embedding, occupation, structured) | `personal_model_state`, `ranking_shadow`, per-run `job_scores.personal_*` | ranking order; lanes; profile *suggestions* | change accept/reject; change stored scores; leave the install; feed P data into E or M |
| **E: Engine** | What makes a good match *in general*? | developer benchmarks (synthetic, volunteer blind labels), knowledge packs | config bundles, knowledge-pack data, code (shipped in releases); `active_strategy` via the lab | scoring, evidence rules, occupation relations, defaults | learn at runtime from one user's signals |
| **M: Market** | What does the market look like? | all stored offers (public data), occupation assignments | `market_*` aggregate tables | occupation relations (as one evidence input, AD-7); insights; profile suggestions; salary hints | decide acceptance; use feedback or profile as input |
| **O: Operational** | Is the pipeline doing its job? | run stats, provenance, decisions, extraction outputs, FT structured fields, canaries | `ops_metrics_daily`, `ops_alerts` | alerts; source pacing (existing breakers/cooldown); proposals to the user or developer | silently change filters, scoring or rankings |

**Allowed flows between planes (and no others):**
1. E → P: the prior (profile embedding, deterministic score), feature definitions, caps.
2. M → E-at-runtime: observed occupation co-occurrence as one *weighted* input of occupation
   relatedness (AD-7). The weight comes from the engine's knowledge pack; minimum support applies.
3. M → P: none directly. M feeds profile *suggestions* only, and the user confirms them.
4. P → O: aggregate counts only, such as "share of user positives that carried weak flag X". They feed
   the systematic-filter-error detector (AD-9). The output is an alert or proposal shown to the same
   user.
5. O → E: developer-facing reports (exported by the user on request, never sent automatically).

**Code-level enforcement** (same technique as `tests/test_evaluation_isolation.py`):
- `src/personal/` (P) imports no network client.
- `src/market/` (M) and `src/ops/` (O) do not import `src/feedback/` except through the one
  aggregate function of flow 4.
- Production does not import `src/evaluation` or `src/lab` (existing).

---

## 3. Decisions

Each decision states: the decision; why; what is reused; schema; conflicts; success criteria;
failure modes and rollback.

---

### AD-1: Shared intelligence travels only in versioned knowledge packs shipped with releases (from D1)

**Decision.**
- In 2.x there is no channel between installs: no telemetry, no server, no federated training.
- Everything shared (occupation taxonomies and crosswalks, vocabularies, default weights,
  classifier centroids, calibration tables) ships as a **knowledge pack**: a read-only SQLite file
  `knowledge/occupations.db` plus a manifest (`pack_version`, per-table SHA-256, licences, sources).
- The pack is bundled with the installer when it is ≤ 30 MB compressed. Otherwise it uses the
  first-run download pattern of R-Q4 (size shown, SHA-256 checked, resume, works without it).
- `pack_version` enters the strategy fingerprint (`src/decisions/strategy.py`, `extra`).
- Opt-in aggregate telemetry is **out of scope**. It may only be proposed later in a separate ADR
  with a privacy design.

**Why.** It meets O-4 and the no-server constraint. Engine improvements stay developer-validated
through the lab (ADR 0011).

**Reuses.** The `esco.db` pattern (separate read-only DB); the R-Q4 download and SHA-256 component
(P3.3); the strategy fingerprint.

**Schema.** New file, not part of `jobs.db` (see AD-7 for its tables). No migration of user data.

**Conflicts.** None. It extends ADR 0007/0011 practice (evaluation outside production).

**Success criteria.**
- The pack loads in ≤ 1 s.
- A missing or corrupt pack is detected by hash, and the app falls back to the current keyword
  vocabularies with a visible notice.
- The fingerprint changes when the pack changes.

**Failure modes / rollback.**
- Bad pack: the hash check fails, so the pack is not used and the fallback applies.
- Wrong content: the release is rolled back by shipping the previous pack.
- `active_strategy` rollback (existing) covers config bundles.

---

### AD-2: Three evidence strengths; only strong constraints reject (from D2)

**Decision.** Every hard-filter check (`src/pipeline/filter.py::_FILTER_CHECKS`) and every
requirements-gate rule produces one of three outcomes:

- **STRONG violation → reject** (as today). Both conditions must hold:
  1. the constraint is an **explicit choice the user made** in the profile, not a curated default; and
  2. the offer's value is **known with high confidence**: a structured source field, or a rule whose
     precision is measured ≥ 0.95 on the synthetic + regression sets.
- **WEAK violation → keep, penalise, flag.** The offer reaches scoring. It carries a `weak_flags` entry
  (code, observed, expected) and a bounded penalty, and it is routed by AD-2b.
- **UNKNOWN → neutral.** No penalty beyond the factor's existing "undetermined" value. This is never a
  rejection (already the rule in ADR 0004 and 0013).

**Classification of current checks:**

| Check | STRONG when | WEAK when | UNKNOWN when |
|---|---|---|---|
| `location` / `distance` / `remote_policy` | exactly as ADR 0013 already defines (strict distance + exact commune; remote residency outside FR/EU explicitly) | outside radius without `strict_distance`, or département-level precision | place unresolved |
| `remote` / `work_mode` | user excluded the mode **and** the mode comes from a structured field or explicit wording ("100 % présentiel") | mode inferred from weak wording | not stated |
| `contract_type` | user excluded the contract **and** it comes from a structured field (`typeContrat`, `employment_type`) | detected only from title words ("freelance" in the title) | not stated |
| `salary` | user set a minimum **and** marked it strict **and** the stated maximum < minimum | stated maximum < minimum, not strict | no salary stated (most offers) |
| `seniority` | never STRONG on title words | title contains a seniority word excluded by the user (senior, lead, manager…) | — |
| `published_age` | never STRONG; a **closed** offer (`valid_through` passed, `expired_at` set: ADR 0012 and expiry #88) stays excluded as today | age > `freshness_days`: penalty grows linearly from 0 at `freshness_days` to the maximum at 3 × `freshness_days` | no plausible date and no `first_seen_at` |
| `title_keywords` (curated domain/role words, curated non-IT exclusions) | only **phrases the user typed** as exclusions ("never show: commercial") | no role word found; curated off-domain word found | empty title |
| Requirements gate: education / experience / principal & mandatory skill (ADR 0004) | unchanged: mandatory wording above the profile's ceiling | preferred wording (existing soft penalties) | not stated |
| Scoring domain gate (`domain_cap`, ADR 0003) | becomes AD-7's "unrelated occupation with high confidence and not targeted" | — | occupation unknown → current 0.45 |

**AD-2b: preventing flooding.** WEAK-flagged offers never enter the main list by default. They are
routed like this:
- **Main list ("Strong matches")**: offers with no weak flag and a final score ≥ the `relevant`
  tier (60), as today.
- **"Outside your criteria"**, part of the planned Home "Worth a look" section (P6):
  - Contents: weak-flagged offers with final score ≥ 60 **and** occupation proximity ≥ 0.6 (AD-7;
    until AD-7 lands, current `domain_relevance` ≥ 0.7).
  - Size: capped at `outside_lane_size` = 10 per run, best first.
  - Each card names the criterion it misses ("36 days old, your limit is 30").
  - Collapsed by default. It never sends notifications.
- **Exception: a learned promotion into the main list.** A weak-flagged offer enters the main list
  only when the personal model (AD-3) gives it a positive adjustment and its final score stays ≥ 60.
  It is labelled "shown because you liked similar offers despite: <criterion>".
- Everything else stays stored and journaled as `accepted` with `lane = "below_threshold"`. It is not
  shown, and it can be audited and re-ranked later. This preserves "never silently discard".
- **Penalties.** Each weak flag subtracts up to 0.10 from the deterministic composite. At most 0.25 in
  total, applied before the ADR 0003 gates. Values are shipped in `scoring.yaml` and tuned in the lab.
- **Feature flag.** `filters.weak_evidence_mode: reject | rank` defaults to `reject` until the
  pre-registered evaluation passes (success criteria below). Each check can be switched separately so
  the rollout can go one check at a time (`published_age` first, then `title_keywords`, then
  `seniority`).

**Why.** In the owner's labels, 5 of 7 relevant tuning offers and 3 of 4 relevant held-out offers
were rejected by weak checks (age, title words). Nothing downstream can recover them. The routing
caps what the user sees.

**Reuses.** `FilterCheckOutcome` (observed and expected already computed); decision journal
`EvidenceItem` (three-valued state); ADR 0004/0013 rule shape; `score_to_tier`; the Home section
design from P6.

**Schema (additive).**
- `job_decisions.evidence_json`: each item gains `strength: "strong" | "weak" | null`;
  `exposure_json` gains `lane`. Decision `schema_version` goes from n to n+1; readers treat a missing
  field as legacy.
- `job_scores.score_breakdown_json` gains `weak_flags` and `weak_penalty`.
- No new table.

**Conflicts.**
- ADR 0001 (wording "hard constraint filtering" → "strong-constraint filtering"; principle kept).
- ADR 0003 (the domain gate basis changes with AD-7).
- ADR 0012 (age becomes graded evidence; expiry stays exclusion).
- Completion plan P4 ("improvements through ranking, not new hard rejections"): compatible, it goes
  further.
- The **filter-semantics change is a global strategy change**: it goes through pre-registration +
  lab + human approval (ADR 0011). The real-v3 pre-registration (#109) covers ranking candidates
  only, so a new dated pre-registration file is needed before the real-v3 held-out is opened for this.

**Success criteria** (pre-registered; tuning = exploratory, held-out = decisive):
1. Relevant offers reaching main ∪ outside-lane: ≥ 2× baseline on real-v2 tuning (baseline 2 of 7),
   and on real-v3 held-out once labelled.
2. Main-list precision (labelled relevant / labelled shown) is not lower than baseline beyond the
   paired-bootstrap 95 % CI.
3. Outside-lane size ≤ `outside_lane_size` in 100 % of runs (enforced; tested).
4. Synthetic self-check: 0 new acceptances of a STRONG violation; 0 changes to strong rejections; the
   ADR 0004 safety cases are unchanged.
5. Pipeline runtime increase ≤ 25 % on a replay of the owner's DB copy. More offers reach scoring and
   embedding; real-v1 had about 14k weak-reason rejections in its pool.

**Failure modes → safeguards → rollback.**
- *Lane fills with junk* → occupation-proximity floor, score floor, size cap; O-plane metric "lane
  open rate" (AD-9).
- *Runtime or disk growth* → `below_threshold` offers get no embedding until needed (lazy); the
  existing cleanup removes expired offers.
- *A weak check was actually protective* → per-check flag back to `reject`.
- Global rollback: `weak_evidence_mode: reject`, or the lab `rollback` of the bundle.

---

### AD-3: Personal preference model with profile prior, bounded and reason-aware (from D3 and D5)

**Decision.** Replace the token-average engine (`src/personalization/engine.py`) by a per-user
Bayesian linear preference model over existing features. It runs at the ranking stage, after
semantic matching, where personalization runs today. Explicit feedback and behaviour change the
ranking **by default** (after AD-5's shadow phase), bounded, visible and undoable.

**AD-3.1 Signal interpretation (one aggregated value per offer).**

Exposure context: an offer can only produce an implicit signal after it was shown (`exposure_log`,
AD-6).

| User action | Event (class) | Meaning for learning | Base weight `w` | Target |
|---|---|---|---|---|
| Shown in a list | `impression` (exposure) | not a preference; propensity only | 0 | — |
| Shown at position ≤ 5 in ≥ 3 distinct sessions, never opened | derived `skip` | weak "not attractive" | −0.15 | embedding + occupation |
| Detail opened | `viewed` (behavior) | curiosity, **not** approval | +0.25 (once per offer) | embedding + occupation |
| Saved | `saved` (behavior, new) or status `to_apply` | intends to consider | +1.0 | all features |
| Apply link opened | `apply_link_opened` (behavior) | strong intent | +1.2 | all features |
| Status applied | outcome `applied` | committed | +1.5 | all features |
| Status interview / offer / accepted_offer | outcome | strongest evidence of fit | +2.0 | all features |
| Status employer_rejected | outcome | employer's choice, not the user's preference | 0 (keeps the earlier positive) | — |
| Status withdrawn | outcome | user stopped pursuing | cancels the outcome positive; no negative | — |
| "Intéressé" | explicit `interested` | stated liking | +1.2 | all features |
| "Intéressant malgré le score" (`interesting_despite_low_score`) | explicit | stated liking + under-scoring | +1.2 | all features; counts for AD-9 |
| Hidden | `hidden` (behavior, new) | do not show again | −1.0 | embedding + occupation; offer removed from the user's lists |
| "Pas intéressé" with no reason / `other` | explicit `not_interested` | stated dislike | −1.2 | embedding + occupation |
| "Pas intéressé" + `too_far` | explicit | dislikes **the distance**, not the job | −1.2 | **distance feature only** |
| + `salary` | explicit | salary | −1.2 | salary feature only |
| + `contract_type` | explicit | contract | −1.2 | contract feature only |
| + `too_senior` | explicit | level | −1.2 | seniority feature only |
| + `insufficient_skills` | explicit | requirements | −1.2 | skills-gap feature only |
| + `off_domain` | explicit | occupation | −1.2 | occupation + embedding |
| + `mis_ranked` | explicit | ranking was wrong (direction from the score) | ±1.0 | all features; counts for AD-9 |
| "Je ne sais pas" | explicit `unsure` | no stance | 0; **clears** an earlier explicit stance | — |
| Calibration label (Quality › Calibration) | separate store | stated relevance | ±1.2 | all features. **Tuning split only; held-out labels are never read** (prevents evaluation leakage) |

**Aggregation per offer** (deterministic, so a repeated click counts once):
1. Precedence: `hidden` > latest explicit stance (`interested` / `not_interested` / `unsure`) >
   outcome > `saved` / `apply_link_opened` > `viewed` > `skip`.
2. The offer's value `y` is the precedence winner's weight. Lower-precedence behaviour adds at most
   +0.25 to a positive winner and nothing to a negative winner.
3. Clamp `y` to [−2, +2].
4. Recency: × the existing half-life factor (90 days, floor 0.05; `config/personalization.yaml`).
5. Profile-change discount: signals older than the last profile save that changed targets or
   occupations count × 0.5. Goals can change.
6. Implicit signals (`viewed`, `skip`) are weighted by inverse propensity of their display position,
   `1/p(k)` with `p(k) = 1/log2(k+1)`, clipped to [1, 3]. Explicit and outcome signals are not
   reweighted.

**AD-3.2 Model.**
- Features `x_j`:
  - `e_j`: the stored 384-d MiniLM embedding, L2-normalised (`job_embeddings`);
  - `o_j`: occupation-group indicators (AD-7, top-level ROME *domaine* / ESCO ISCO 2-digit, at most
    about 110 values);
  - `s_j`: structured features: distance bucket (5), remote kind (4), contract (6), seniority level
    (6), salary bucket (5), source (≤ 15), weak-flag codes (≤ 6).
- Embedding part: a preference vector `u_e` with prior mean = the normalised **profile embedding**
  (`build_profile_embedding`) and isotropic prior precision `α_e`. Posterior by ridge / Bayesian
  linear regression on signals routed to the embedding (reason routing above):
  `u_e = (α_e I + Σ c_j e_j e_jᵀ)⁻¹ (α_e u_0 + Σ c_j y_j e_j)`, with `c_j` = |weight| × propensity.
  The 384×384 solve takes milliseconds.
- Structured and occupation parts: per-value Beta-like mean adjustments with prior 0 and pseudo-count
  `κ` (shrinkage): `adj(v) = Σ y / (κ + n_v)`. Only signals routed to that feature count.
- Personal adjustment of offer `j`:
  `Δ_j = clip( β·(u_e·e_j − u_0·e_j) + Σ_v adj(v) , −cap(n), +cap(n) )`
- **Evidence-scaled cap**: `cap(n) = 0.30 · n / (n + 10)`, where `n` is the number of offers with a
  non-zero `y`. One signal → ±0.027; 10 → ±0.15; 40 → ±0.24; the maximum is 0.30 (today's
  `max_abs_adjustment`). **No minimum count, no threshold.**
- `final = deterministic_blend + Δ_j`, then `cap_personalization_at_gates()` is re-applied (ADR
  0002/0003).
- With `n = 0`, `Δ ≡ 0` → INV-7.
- Initial values, shipped in `config/personalization.yaml` and tunable only through the lab:
  `β = 1`, `α_e = 4`, `κ = 3`.

**AD-3.3 Updates.** The state is recomputed from the journals:
- at each pipeline run;
- after each new explicit, outcome, saved or hidden event (debounced 2 s; cheap).

`personal_model_state` stores the posterior plus a `state_hash` over the inputs, so it can be checked
and rebuilt (INV-4). `personal_model_version` = algorithm version + config hash + state hash.

**AD-3.4 Exploration** (from D8). In each run's top 20, up to 2 slots are "À découvrir" picks:
- Selection: Thompson sample from the embedding posterior, among offers that pass strong constraints,
  score ≥ 50, were not shown in the last 7 days, and have the highest posterior variance.
- Labelled in the UI. Never from strong-rejected offers.
- Disabled by the health monitor while personalization is paused (AD-5).

**AD-3.5 Diversity guard.** Within the top 20, at most 6 offers share one occupation group, and at
most 3 share one company, unless the user's positive signals are mainly in that group. This stops one
early click from taking over the list.

**AD-3.6 Transparency.**
- **"Ce que l'application a appris" panel** (Settings › Personalization and the Offer page):
  - each top learned effect in words ("Plus d'offres comme celles que vous avez enregistrées en
    support applicatif"), with the offers that caused it;
  - per-offer explanation ("+0.08 : proche de 3 offres enregistrées");
  - **Reset learning**;
  - **pause**.
- No sliders, no numbers to configure.

**Reuses.**
- `PersonalizationStorage` cache pattern;
- `PersonalizationExplanation`;
- recency decay;
- `cap_personalization_at_gates`;
- `job_embeddings`;
- `build_profile_embedding`;
- `FeedbackService.canonical_for`;
- the disagreement engine's user stance;
- `job_scores`.

The legacy `job_feedback` table and `personalization_bridge` are retired. The bridge rows stay
readable for one release, then are dropped through a named data migration.

**Schema.**
- `personal_model_state(user_scope TEXT PK DEFAULT 'local', version TEXT, state_hash TEXT,
  computed_at TEXT, n_signals INT, state_blob BLOB, paused INT, reset_at TEXT)`.
- `job_scores` gains columns `personal_adjustment REAL`, `personal_model_version TEXT`,
  `lane TEXT`, added with `ensure_columns`.

**Conflicts.**
- ADR 0009 ("explicit feedback does not change rankings by default; bridge opt-in"): **reversed for
  ranking**. Explicit feedback still never changes acceptance.
- ADR 0002 consequence "anything that needs *the* score reads the stored deterministic score": Home
  ordering and lanes read the per-run final score. Tiers stay deterministic.
- Strategy fingerprint: covers the personalization *config and algorithm version*, not the learned
  state. Every click must not create a new strategy. The per-run `personal_model_version` is recorded
  in `job_scores` and in decision `signals`.

**Success criteria.** See AD-5 for the shadow and health metrics. Offline, before the shadow:
- **Simulated-user learning curve** on real-v2 tuning labels (69 labels, the owner as a simulated
  user, 200 random feeding orders):
  - mean AUC of the ranking among the remaining labelled offers is non-decreasing in k = 0, 3, 5, 10,
    20 fed labels;
  - at no k is it below the k = 0 (prior) AUC by more than the bootstrap CI;
  - the k = 0 ranking is identical to production.
- **Reset**: after reset the ranking is bit-identical to the no-signal ranking.
- **Latency**: model update ≤ 200 ms; scoring ≤ 1 ms per offer for 5,000 offers on the reference
  laptop.

**Failure modes → safeguards → rollback.** See §10. Rollback: pause or reset (user); auto-pause
(AD-5); algorithm rollback = `personalization.algorithm: legacy | bayes_v1` (kept for one release).

---

### AD-4: Two promotion regimes: personal learning updates itself within bounds; global changes need the lab (from D4)

**Decision.**
- **Personal plane (Layer 1)**: the posterior updates continuously within `cap(n)` (AD-3). It is
  *not* a strategy candidate and needs no lab or approval, because it is bounded, explainable,
  reversible and guarded by AD-5.
- **Personal algorithm or config changes** (new feature, new weights, new cap formula) arrive only in
  releases. On install they first run as a *shadow challenger* (AD-5). They become active per install
  only when the challenger beats or matches the active one on that user's signals, or after 30 days
  with no evidence against it and guardrails green.
- **Engine plane (Layer 2)**: filters, scoring, occupation relations, evidence rules and knowledge
  packs. These are **unchanged**: lab, pre-registration, held-out developer benchmarks, human
  `approve` on the CLI (ADR 0011; owner decision I3).

**Why.** One user's data can never meet the lab's evidence floor (30 labelled, 5 discordant). A
bounded personal adjustment does not need that floor; a global rule change does.

**Reuses.** ADR 0011 machinery unchanged for E; AD-5 for P.

**Schema.** None beyond AD-3 and AD-5.

**Conflicts.**
- ADR 0011 needs an amendment paragraph: "Layer-1 personal learning is outside the lab's scope; it is
  governed by ADR 00xx (AD-3/4/5)".
- Completion plan P4 ("personalization measured on held-out labels before being on by default",
  plan C10): replaced. Held-out labels remain the **developer** release gate for the *algorithm*
  (AD-3 offline criteria). Per-user activation is governed by AD-5's behavioural shadow.

**Success criteria.**
- No personal change can cross a strong constraint (property test over random signal sequences).
- Every global change still has a lab record.

**Rollback.** As in AD-3 and ADR 0011.

---

### AD-5: Shadow evaluation and a self-reverting health monitor for personal ranking

**Decision.**
- **Shadow pairs.** Every run computes and stores, without showing it, the ranking of an *alternative*
  next to the production ranking (top 100 ids, positions, scores) in `ranking_shadow`:
  - before AD-3 is active: production (prior) vs personalized challenger;
  - after: personalized (active) vs prior baseline; plus any release challenger.
- **Evaluation unit: discordant positives.** A positive is an offer that later gets aggregated
  `y ≥ +1.0` (saved, apply link, applied, interested, outcome) within 14 days of the run. It is
  discordant when the two rankings place it ≥ 3 positions apart (positions beyond 100 count as 101).
  Win = the ranking that placed it higher.
- **Why this metric.** It needs no labels and works with a handful of events. It compares the two
  rankings on the same offers, which limits position bias. The production ranking's advantage (the
  user only sees production) is reduced by counting positives found through search, filters and the
  outside lane as well as the main list. The remaining bias favours production, so the test is
  conservative for challengers.
- **Activation rule** (challenger → active):
  - ≥ 8 discordant positives, challenger win rate ≥ 0.60, all guardrails green; **or**
  - 30 days with < 8 discordant positives, win rate ≥ 0.5 on what exists, guardrails green. Low
    activity: the bounded `cap(n)` keeps the risk small.
- **Health monitor** (active personalized vs prior), rolling 30 days:
  - win rate ≤ 0.40 with ≥ 8 discordant → halve `cap`. A second time → **pause personalization**
    (`paused = 1`) and show "Personnalisation en pause : elle n'améliorait pas vos résultats" with
    resume and reset buttons.
  - top-20 positive rate (positives per run in the top 20) over 14 days drops > 50 % vs the trailing
    60-day median, with ≥ 6 positives in the baseline → same response.
- **Guardrails** (checked every run; a failure blocks activation and triggers a pause):
  - 0 strong-constraint violations shown;
  - diversity guard respected;
  - lane cap respected;
  - main-list size within ±30 % of the prior ranking's main-list size.

**Reuses.** Decision journal exposure; `job_scores`; `run_id`; scheduler; Quality page cards.

**Schema.**
- `ranking_shadow(run_id TEXT, variant TEXT, model_version TEXT, ranks_blob BLOB, created_at TEXT,
  PRIMARY KEY(run_id, variant))`. Retention 90 days.
- `personal_health(day TEXT PK, discordant INT, wins INT, top20_pos INT, action TEXT)`.

**Conflicts.** None. ADR 0010's shadow habit is extended.

**Success criteria.**
- Fault-injection tests: an adversarial signal sequence (positives on offers the "true" preference
  dislikes) triggers cap halving, then a pause, within the configured windows.
- Normal replay of the owner's history causes no pause.

**Rollback.** The pause is automatic. Resume and reset are the user's. Release rollback of the
algorithm (AD-3).

---

### AD-6: Signal capture: exposure log, Save and Hide, with no new mandatory step

**Decision.**
- **Impressions** go into `exposure_log`. This is a separate table, not `feedback_events`: volume is
  high and it must be prunable. One row per (session, list, job) with best position and first shown
  time. Lists: `home_strong`, `home_outside`, `home_explore`, `jobs_search`, `jobs_filtered`. Batched
  from the frontend (`navigator.sendBeacon`, at most one request every 5 s). Fire-and-forget like
  `recordJobEvent`.
- **New behaviour types** in `feedback_events` (`BEHAVIOR_TYPES`): `saved`, `unsaved`, `hidden`,
  `unhidden`. One-click actions on every list card and on the Offer page.
- `to_apply` status counts as `saved` for learning (no duplicate UI concept). The application tracker
  stays as it is.
- "Votre avis" stays as it is: optional, with reasons. Its effect becomes visible (AD-3.6).
- No counters, quotas or "label N offers" prompts anywhere.
- **Active questions (optional).** At most one per Home visit, at most 3 per week, dismissible
  forever. It asks "Cette offre vous intéresse ?" only about the offer with the **highest posterior
  variance** among the top 30 (AD-3). One answer to an uncertain offer is worth several random
  labels.

**Reuses.** The `feedback_events` append-only journal (ADR 0009), the `recordJobEvent` client, and the
decision linkage (`decision_id`, `strategy_version`).

**Schema.**
- `feedback_events`: vocabulary only (`FEEDBACK_SCHEMA_VERSION` +1), no column change.
- New table `exposure_log(session_id TEXT, list TEXT, job_id TEXT, best_position INT, first_shown_at
  TEXT, run_id TEXT, PRIMARY KEY(session_id, list, job_id))`, index on `(job_id)`. Retention 180 days
  (AD-12).

**Conflicts.** ADR 0009 is extended, not changed. Cleanup `PROTECTING_TABLES` gains nothing: an
impression does not protect an offer from removal. A save or hide does, since it lives in
`feedback_events`.

**Success criteria.**
- ≥ 99 % of rendered list cards logged (UI test with a mock beacon).
- 0 blocking calls on render.
- No measurable render slowdown (≤ 5 ms).
- Lost impressions on a crash are acceptable; lost explicit events are not (existing fsync and journal
  guarantees).

**Rollback.** Stop logging with a flag. The tables stay and are ignored.

---

### AD-7: Occupation model: ROME (France) + ESCO (Europe) + embeddings, for generalisation and professional potential (from D7)

**Decision.**

**AD-7.1 Identity.**
- Every offer and every profile target is mapped to occupations in **schemes**: `rome` (primary in
  France) and `esco` (European, used for skills and expansion).
- The knowledge pack (AD-1) contains:
  - ROME 4.0 fiches, appellations and skills;
  - ESCO occupations, skills and essential/optional occupation–skill relations;
  - the official ROME↔ESCO crosswalk (France Travail publishes one: *availability and licence to be
    verified before ADR*);
  - precomputed MiniLM embeddings of all occupation labels and alt labels;
  - ISCO grouping.
- Tables are scheme-agnostic: a future scheme (e.g. a German or Spanish taxonomy) is added rows, not
  schema changes (O-4).

**AD-7.2 Assignment (`job_occupations`), in order of confidence:**
1. **Structured**: France Travail `romeCode` / appellation from `raw_payload` → confidence 1.0, method
   `structured`. This data is already stored and unused today.
2. **Classifier**: k-nearest neighbours (k = 15) over stored job embeddings of structured-labelled
   offers (the local FT offers) **plus** occupation label embeddings from the pack (cold start when
   few FT offers exist). Top-3 codes with vote-share confidence. Abstain (`unknown`) below 0.35.
   - No training run; the "model" is the stored embeddings.
   - Measured continuously on held-out FT offers (AD-9: hide the code, predict, compare).
3. Unknown → keyword vocabularies (current `filters.yaml`) as fallback evidence.

**AD-7.3 Profile occupations.**
- Derived from profile v2 `experiences[].role`, `targets` and `looking_for`, by the same classifier
  on their text, then confirmed by the user in the Profile page and the wizard ("Vous visez : Technicien
  support (M1810) ✓ — Administrateur systèmes ✓ — ajouter…").
- Stored in the profile as `targets.occupations: [{scheme, code, label, source: user|suggested}]`.
- This replaces `target_domains` as the primary domain input; `target_domains` stays a fallback for one
  release.

**AD-7.4 Relatedness and reachability (O-1).** For an offer occupation `a` and a profile occupation
`b`:

`related(a,b) = max( official(a,b), w_s · skill_overlap(a,b), w_m · market_cooc(a,b), w_e · cos(centroid_a, centroid_b) )`

- `official`: 1.0 same code; 0.8 same ROME fiche group / ESCO ISCO 4-digit; 0.7 ROME "mobilité"
  link (if present in ROME 4.0 data; *to verify*).
- `skill_overlap`: weighted Jaccard of ESCO essential skills.
- `market_cooc` (from AD-8): how often the two occupations' skills co-occur in local offers, minimum
  support 20.
- `centroid` (from AD-8): mean embedding of local offers of that occupation.
- Weights `w_*` are shipped in the pack and tuned in the lab.

**Reachability** of offer `j` for the user: share of the offer occupation's essential skills (plus
skills extracted from the offer) covered by the user's skills, from experiences and target skills,
with ESCO synonyms. Adjusted by experience and education fit (the existing ADR 0004 evidence).

**AD-7.5 Use in scoring** (engine plane; lab-promoted).
- `domain_relevance` becomes `max_b related(occ_j, b)` over the profile occupations, with confidence
  weighting:
  - unknown occupation → the current 0.45 "undetermined";
  - keyword evidence still lifts to 0.7–1.0 when occupation is unknown.
- The **domain gate** (`domain_cap`, ADR 0003) applies only when the offer occupation has confidence
  ≥ 0.8 **and** `related < 0.2` **and** the user did not target it.
- Professional potential appears as:
  - a tag "Accessible : 7/10 compétences clés";
  - a "Reconversion possible" section inside "Worth a look", offers with `related` 0.5–0.8 and
    reachability ≥ 0.6;
  - never as a gate.

**Why.** It meets O-1 and O-4. It uses data already collected (FT ROME codes, stored embeddings) and
ships taxonomy knowledge without a runtime AI dependency. ESCO's open licence and ROME's open data
status make shipping feasible (*licences to confirm in the ADR*).

**Reuses.**
- `src/esco/` (DB pattern, FTS search; extended into the pack);
- `compute_esco_synonym_map`;
- `job_embeddings`;
- `EmbeddingService`;
- profile v2 `experiences` / `targets`;
- `_score_domain_relevance` (kept as the fallback branch);
- the requirements extractor.

**Schema.**
- Pack (`knowledge/occupations.db`, read-only): `occupation(scheme, code, label_fr, label_en,
  parent_code, isco)`, `appellation(scheme, code, label, lang)`, `skill(uri, label_fr, label_en,
  type)`, `occupation_skill(scheme, code, skill_uri, relation)`, `crosswalk(scheme_a, code_a,
  scheme_b, code_b, kind)`, `relation(scheme, code_a, code_b, kind, weight)`, `label_embedding(scheme,
  code, model_name, vector BLOB)`, `manifest`.
- `jobs.db`: `job_occupations(job_id TEXT, scheme TEXT, rank INT, code TEXT, confidence REAL, method
  TEXT, model_version TEXT, assigned_at TEXT, PRIMARY KEY(job_id, scheme, rank))`, index `(scheme,
  code)`. It is added to cleanup `DERIVED_TABLES`.
- Profile: `targets.occupations` (profile v2 contract; `PROFILE_SCHEMA_VERSION` +1, migration adds an
  empty list plus a review note).
- **Data migration (named, resumable, background)**: assign occupations to existing offers, structured
  first, then classifier, in batches of 2,000 with progress in Settings › Data. Backup first
  (`backup_before_migrating`).

**Conflicts.**
- ADR 0003: domain gate basis.
- Profile v2 / P3.1 and P3.4: the wizard and Profile page need the occupation picker. This is a **P3
  dependency**; it can land in P3 or right after.
- Skill Graph's `KNOWN_TECHNOLOGIES` vocabulary: AD-8 extends it.
- Strategy fingerprint: pack version (AD-1).

**Success criteria.**
- Structured coverage: ≥ 99 % of FT API offers get a ROME code.
- Classifier on held-out FT offers (code hidden):
  - top-1 accuracy ≥ 0.70 and top-3 ≥ 0.90 (initial targets; the first measurement sets the baseline,
    and the targets are confirmed in the ADR);
  - abstention ≤ 25 %;
  - calibration: confidence ≥ 0.8 → accuracy ≥ 0.9.
- **Multi-occupation self-check** (new synthetic set):
  - 6 fixture profiles: nurse, accountant, warehouse operative, sales representative, systems
    administrator, chef;
  - a fixed corpus of 600 fixture offers;
  - ≥ 80 % of each profile's top 20 are target or related occupations;
  - 0 strong-rule regressions.
- The IT profile on real-v2 held-out is not worse than baseline beyond the CI.
- Pack load ≤ 1 s; assignment ≤ 2 ms per offer.

**Failure modes → safeguards → rollback.**
- *Wrong occupation → wrong domain cap*: the cap needs confidence ≥ 0.8, it never applies to targeted
  occupations, and the O-plane accuracy alert covers drift.
- *Sparse classes*: abstain, then keyword fallback.
- *Pack missing*: fallback to the keyword path (today's behaviour).
- Rollback: `scoring.domain_basis: keywords | occupation` (lab bundle) and lab `rollback`.

---

### AD-8: Market plane: aggregate facts from every offer, kept beyond offer deletion

**Decision.** A nightly job (scheduler, after `pipeline_daily`) maintains **aggregate** tables from
all stored offers, accepted or not:
- **`market_occupation_period`** (week × scheme/code × département|region|remote): new offers, active
  offers, salary p25/p50/p75 where stated (with n), experience-required histogram, education-level
  histogram, contract mix, median days to expiry.
- **`market_skill_period`** (week × occupation × skill): count of offers requiring the skill (ESCO
  skill URI when matched, else normalised term).
- **`market_relation`** (occupation × occupation): skill-profile cosine and co-occurrence support;
  the centroid embeddings feed AD-7.4.

Aggregates are computed **before cleanup deletes expired offers** (hook in `src/storage/cleanup.py`).
History therefore survives offer removal.

**How the four sources work together:**

| Source | Role | Limitation it covers for the others |
|---|---|---|
| **ROME** | official French occupation identity, fiches, appellations | gives offers a stable id; FT offers come pre-labelled |
| **ESCO** | multilingual occupations, ~14k skills, essential/optional relations, ISCO | skill identity and official occupation–skill links; expansion beyond France |
| **Stored embeddings** | map unlabelled text (non-FT offers, profile text, free skills) to ROME/ESCO; occupation centroids | coverage where taxonomies have no code or no relation |
| **Skill Graph** (`src/skillgraph/`) | observed demand and co-occurrence in local offers, gaps vs profile | real, current market evidence; becomes the *view* over `market_*` tables with ESCO skills instead of `KNOWN_TECHNOLOGIES` |

**Uses** (all non-deciding):
- Skill Graph v2 and Insights pages (P6);
- profile suggestions (AD-3.6: "Les offres de votre métier demandent souvent : X");
- salary realism hints on the Profile page;
- `market_cooc` and centroids inside AD-7.4 relatedness (engine weight, minimum support 20);
- AD-9 drift detection (sudden occupation-mix shifts).

**Honesty rules.**
- Every market figure says "d'après N offres collectées par l'application", because collection follows
  the user's own sources and queries and is not a census.
- Figures with n < 5 are suppressed.

**Reuses.** Skill Graph query code and the read-only connection pattern; scheduler; cleanup; ESCO
synonyms.

**Schema.** The three tables above in `jobs.db`, plus `market_build(period, built_at, n_offers,
pack_version)`. Additive. Rebuildable from offers still present; older periods are not rebuildable
once offers are deleted, which is why the build runs before cleanup.

**Conflicts.** Skill Graph vocabulary change (internal); cleanup ordering.

**Success criteria.**
- A rebuild on the same data is byte-identical.
- The nightly build takes ≤ 60 s for 100k offers on the reference laptop.
- For non-IT fixture corpora the Skill Graph v2 shows non-IT skills.
- Suppression is enforced (test).

**Failure modes.**
- Biased sample: the disclosure above.
- Build failure: fail-open (yesterday's aggregates stay); O-plane alert.

**Rollback.** Drop the tables; the feature flag hides the pages.

---

### AD-9: Operational plane: self-monitoring with detectors, alerts and proposals; no silent self-modification

**Decision.**
- Daily metrics go into `ops_metrics_daily(day, scope, metric, value, n)`.
- **Detectors**: a robust z-score vs the trailing 14-day median (MAD), a minimum volume per metric,
  and fixed thresholds where stated.
- Alerts go into `ops_alerts`, shown in **Quality › Santé** and Settings › Sources (existing
  surfaces). Each alert has a plain sentence, the evidence and a suggested action.

| Concern (O-3) | Metric | Detector | Action |
|---|---|---|---|
| Source degradation | per source: offers collected, new offers, parse errors, canary result, breaker trips | volume or new-offer z < −3 for 2 runs; canary fail (existing) | alert; existing breaker/cooldown |
| Field extraction failures | per source: completeness of title, description, date, company, place, contract | drop ≥ 20 points vs median | alert "source X a changé de format" |
| Requirement extraction quality | **extractor vs FT structured** (experience years, education level): precision / recall / disagreement rate, weekly | precision < 0.85 or a drop ≥ 10 points | alert (developer-facing export); the disagreeing field is treated as UNKNOWN for that offer |
| Occupation classifier | held-out FT accuracy (AD-7) weekly | top-1 drop ≥ 10 points | alert; the domain cap needs confidence ≥ 0.8 anyway |
| Duplicate growth | exact duplicate share; near-duplicate candidate share (embedding cos ≥ 0.95 + same company/place); reposts | share z > 3 | alert; near-duplicates are **linked** ("probablement la même offre"), never auto-merged in 2.x |
| Systematic filtering errors | per weak-flag code and strong check: share of offers flagged; **share of user positives carrying that flag** (flow 4) | ≥ 3 positives in 30 days with the flag, or > 30 % of positives | proposal to the user: "Vous retenez souvent des offres de plus de 30 jours : allonger la limite ?" (one click edits the profile) |
| Strong-rule safety | rejections per strong rule; user positives on offers later found in the rejected pool (via search) | any positive on a strong-rejected offer | alert + `review` (ADR 0010 v2 already flags this in shadow) |
| Ranking regressions | AD-5 health metrics; synthetic self-check after every release or bundle change (existing) | AD-5 rules; self-check FAIL | AD-5 auto-pause; self-check failure blocks bundle promotion (existing) |
| Freshness / expiry | expiry rate per source; offers active > 120 days | z > 3 | alert |

**Automatic actions allowed:**
- existing breakers and pacing;
- treating disagreeing extractions as UNKNOWN;
- AD-5 pauses.

Everything else is an alert or a proposal.

**Reuses.**
- `pipeline_runs.stats_json` (`by_source`, `per_query_stats`);
- `source_breakers`;
- `canary.py`;
- `job_provenance`;
- `job_decisions`;
- `quality_jobs`;
- the self-check;
- Quality and Settings pages.

**Schema.**
- `ops_metrics_daily(day TEXT, scope TEXT, metric TEXT, value REAL, n INT, PRIMARY KEY(day, scope,
  metric))`.
- `ops_alerts(alert_id TEXT PK, raised_at TEXT, scope TEXT, code TEXT, severity TEXT, evidence_json
  TEXT, status TEXT, resolved_at TEXT)`.
- Retention: metrics 400 days, alerts 400 days.

**Conflicts.** None. It folds the current Quality "personal job + FP by reason/source" (#104) into
one surface.

**Success criteria.**
- Each detector raises within one run on its injected fault: a source returns 0; completeness drops;
  duplicates spike; the share of positives with a flag crosses the threshold; the extractor is
  degraded.
- Replay of the owner's last 30 days of run history: ≤ 1 false alarm per week.

**Failure modes.**
- Alert fatigue: minimum volumes, a 2-run persistence rule, a daily digest grouping alerts.
- Detector crash: fail-open, logged.

**Rollback.** Flag off; tables ignored.

---

### AD-10: Labels are developer and release instruments, never a user prerequisite (from D6)

**Decision.**
- The app never asks for a minimum number of labels and never conditions quality or features on
  labels.
- Quality › Calibration stays available as an optional "Aider à mesurer la qualité" tool.
- Uses of labels:
  1. developer regression and release gates (synthetic, volunteer blind sets, owner sets);
  2. pre-registered decisions on engine changes;
  3. offline simulated-user tests of AD-3;
  4. the user's own **tuning-split** calibration answers count as explicit signals (AD-3.1).
     Held-out answers are never read by P.
- A **volunteer blind-label programme** (process, not code): 3 or more volunteers in different
  occupations each label one export of 100 offers. These become developer datasets under
  `data/benchmarks/` on the developer machine, never shipped.

**Conflicts.** It amends roadmap item F0.2 / R-Q5 wording: the owner's 100 labels were the first
developer dataset, not a product prerequisite. R-C1 stands for engine changes.

**Success criteria.** No UI string or flow requires labels (UI test); the evaluation isolation test is
unchanged.

---

### AD-11: Exploration and lanes are part of the product surface (from D8)

**Decision.** Home sections, aligned with P6:
- **"Meilleures offres"**: main list.
- **"À regarder"**: outside-criteria lane (AD-2b, ≤ 10) + "Reconversion possible" (AD-7.4) +
  exploration picks (AD-3.4, ≤ 2 inside the top 20 of the main list, labelled).
- **"À vous de juger"**: disagreement `review` items (ADR 0010, after its own calibration) + the
  optional active question (AD-6).

Each section has a one-line explanation and a "why this offer" link.

**Success criteria.**
- Lane caps are enforced.
- Usability test (R-A3): 3 of 3 testers can say why an offer is in "À regarder".
- "À regarder" open rate ≥ 25 % of the main list's open rate after 30 days, as an O-plane metric. If it
  is lower, the lane is shrunk automatically to 5.

---

### AD-12: Privacy and retention (from D9)

**Decision.**

| Data | Where | Personal? | Retention | User control |
|---|---|---|---|---|
| Offers, provenance, decisions, snapshots | `jobs.db` | public data + the user's decisions | as today (ADR 0008: text 60 d, records 365 d; cleanup) | Settings › Data |
| `feedback_events` (explicit, behaviour, outcome, saved/hidden) | `jobs.db` | **yes** | until erased | **Reset learning** (non-destructive: appends a `learning_reset` marker; earlier signals are ignored) and **Erase my activity** (destructive, backup first) |
| `exposure_log` | `jobs.db` | yes (browsing) | 180 days rolling | erased with activity |
| `personal_model_state`, `ranking_shadow`, `personal_health` | `jobs.db` | yes (derived) | state: current only; shadow 90 d; health 400 d | erased with activity; rebuilt otherwise |
| `market_*`, `ops_*` | `jobs.db` | indirectly (reflects the user's searches) | 400 d (market periods indefinitely, aggregates only) | "Erase all derived data" in Settings › Data |
| Knowledge pack | install folder | no | per release | — |
| Logs | `logs/` | must not contain notes, preference vectors or profile text | as today | — |

**Rules.**
- No network transmission of anything in this table (INV-2; test: P, M and O modules import no HTTP
  client, and an integration test with a socket guard during a full run).
- Settings › Data **export** includes learned preferences in readable form (portability).
- **Erasure vs append-only** (ADR 0009 amendment): erasure of `feedback_events` becomes an
  application action. It uses a dedicated maintenance path:
  1. backup first;
  2. drop the triggers inside one transaction;
  3. delete;
  4. recreate the triggers;
  5. record an `erasure` marker without content.
  "Reset" stays the normal, non-destructive choice.
- Free-text notes stay local and are never used as features (no NLP on notes).

**Conflicts.** ADR 0009 ("erasure is a deliberate owner-side DB operation"): amended for a public app.

---

### AD-13: Roadmap re-scope (from D10)

**Decision.**
- P3 continues, and gains AD-7.3's occupation picker in the Profile page and wizard (or immediately
  after P3).
- **P4 "ranking quality" is replaced** by stages L1–L4 (§9).
- P5 (AI analyst, on demand) is unchanged. Phase-2 top-100 enrichment retirement is unchanged. LLM
  stays optional and is not used by any AD here.
- P6 Home sections follow AD-11.
- P7 release gates add: AD-5 health monitor fault tests; INV-2 socket test; multi-occupation
  self-check; lane caps; reset bit-identity.

---

## 4. Dependencies between decisions

```text
AD-1 (packs) ──► AD-7 (occupations) ──► AD-8 (market) ──► AD-7.4 market terms
                     │                       │
AD-6 (signals) ──► AD-3 (personal model) ──► AD-5 (shadow/health) ──► AD-4 (activation regime)
     │                 ▲                          │
     │                 └── AD-7 occupation features (optional at first: embedding + structured only)
     └──► AD-9 flow-4 metrics
AD-2 (weak evidence) ── needs: lane UI (AD-11), occupation floor (AD-7, or domain_relevance ≥ 0.7 meanwhile),
                        pre-registration + lab (ADR 0011)
AD-9 (ops) ── extractor check needs FT structured fields (AD-7.2 step 1); flow-4 needs AD-6
AD-10, AD-12 ── no code dependency; AD-12 must be approved before AD-6 ships
AD-13 ── after the others are approved
```

**Critical path:** AD-12 approval → AD-6 → AD-3 in shadow → AD-5 → personal on by default.
**Parallel path:** AD-1 → AD-7 → AD-8. **AD-2** can start as soon as its pre-registration is written,
using the interim floor.

---

## 5. Conflicts with existing ADRs and the 2.0 plan: summary

| Item | Nature | Resolution |
|---|---|---|
| ADR 0001 | wording | "hard constraint" → "strong constraint" (AD-2); principle kept |
| ADR 0002 | consequence amended | Home and lanes read the per-run final score; stored score stays deterministic (AD-3) |
| ADR 0003 | domain gate basis; weak penalties added before the gates | AD-2, AD-7.5 via the lab |
| ADR 0004 | none | its rules are the template for STRONG |
| ADR 0005 / 0013 | none | already "uncertainty never rejects" |
| ADR 0008 | additive fields | `strength`, `lane`, `personal_model_version`; schema_version +1 |
| ADR 0009 | **reversal**: feedback changes ranking by default; erasure in app | AD-3, AD-12 |
| ADR 0010 | extended | its `review` items feed "À vous de juger" |
| ADR 0011 | **amendment**: personal learning out of lab scope | AD-4 |
| ADR 0012 | age graded | AD-2; expiry still excludes |
| Completion plan P4 / C10 | replaced | AD-13 |
| R-C1 (held-out before matching changes) | kept for engine changes; personal activation uses behaviour | AD-4, AD-5 |
| R-C2 / R-C5 (UI, no terminal) | respected | every AD ships UI: learned panel, lanes, health, Save/Hide |
| F0.2 / R-Q5 | reframed | AD-10 |
| real-v3 pre-registration (#109) | does not cover AD-2 | new pre-registration file before use |
| Strategy fingerprint hashes config bytes, comments included | trap | new config keys in their own PR with re-recorded golden fingerprint |
| Cleanup deletes expired offers | ordering | market build before cleanup (AD-8) |

---

## 6. Reuse inventory (exact)

| Existing component | Reused for |
|---|---|
| `src/feedback/` (journal, service, `canonical_for`, vocab) | AD-3 signals, AD-6 types, AD-12 erase/reset |
| `src/personalization/` (cache, explanation, recency, config, `cap_personalization_at_gates`) | AD-3 (engine replaced; scaffolding kept) |
| `src/pipeline/semantic_matching.py`, `src/embeddings/` (ONNX), `job_embeddings` | AD-3 features, AD-7 classifier, AD-8 centroids, AD-9 near-duplicates |
| `src/pipeline/filter.py` (`FilterCheckOutcome`, `filter_expected`) | AD-2 classification |
| `src/requirements/` (gate, extractor, education) | AD-2 STRONG rules; AD-9 extractor check; AD-7.4 reachability |
| `src/decisions/` (recorder, evidence, disagreement, strategy) | AD-2 fields, AD-5 attribution, AD-11 review lane |
| `src/evaluation/` (metrics with CIs, held-out, calibration, self-check) | AD-3 offline curve, AD-7 self-check, release gates (dev side only) |
| `src/lab/` | AD-2, AD-7.5, pack weights: global promotion |
| `src/esco/` | AD-7 pack structure, synonyms |
| `src/skillgraph/` | AD-8 view layer |
| `src/discovery/adaptive.py` | AD-9 query value (yield → downstream positives) |
| `src/scheduler/` | nightly market build, ops metrics, model refresh |
| `src/storage/migrations.py` (`ensure_columns`, data-migration ledger, backup) | every schema change |
| `src/storage/cleanup.py` | derived tables; market build hook |
| `src/collectors/canary.py`, `source_breakers`, `job_provenance` | AD-9 |
| Quality page, Settings › Sources/Data, Offer page, P6 Home | UI surfaces |
| R-Q4 download component (P3.3) | knowledge pack download if too large to bundle |

---

## 7. Schema and migration summary

All changes are additive. They go through `src/storage/migrations.py`: `ensure_columns` for columns,
named `DataMigration` entries for backfills, and `backup_before_migrating` first. An older app version
ignores unknown tables and columns. Each change gets its own PR and test with a temporary DB.

| Change | DB | Type | Backfill |
|---|---|---|---|
| `exposure_log` | jobs.db | new table | none |
| `feedback_events` vocabulary (`saved`, `unsaved`, `hidden`, `unhidden`, `learning_reset`, `erasure`) | jobs.db | vocab, schema_version +1 | none |
| `job_scores.personal_adjustment`, `personal_model_version`, `lane` | jobs.db | columns | none (null = legacy) |
| `personal_model_state`, `ranking_shadow`, `personal_health` | jobs.db | new tables | state rebuilt from the journal on first run |
| `job_decisions` evidence `strength`, exposure `lane` | jobs.db | JSON fields, schema_version +1 | none |
| `job_occupations` | jobs.db | new table (+ cleanup derived) | **named resumable migration** from `raw_payload` + classifier, background, progress UI |
| `market_occupation_period`, `market_skill_period`, `market_relation`, `market_build` | jobs.db | new tables | first nightly build over offers present |
| `ops_metrics_daily`, `ops_alerts` | jobs.db | new tables | optional 30-day backfill from `pipeline_runs.stats_json` |
| `knowledge/occupations.db` | new file | read-only pack | shipped |
| Profile `targets.occupations` | profile YAML | profile v2 contract +1 | migration adds `[]` + review note; suggestions shown |
| Retire `job_feedback` bridge rows | jobs.db | named migration, one release later | — |
| Feedback erasure path | jobs.db | maintenance function (drop/recreate triggers in one transaction) | — |

`job_embeddings.vector` stays TEXT (JSON). It is parsed for the candidate set only (≤ 10k vectors/run,
acceptable). A BLOB migration is **not** proposed unless profiling shows the need.

---

## 8. Learning with very little data, cold start, and avoiding self-reinforcement

**Cold start (day 0).**
- Profile prior + occupation identity (pack) + deterministic scoring = today's quality, and better
  for non-IT profiles once AD-7 is in.
- Personal adjustment = 0 (INV-7). Market tables are empty until the first nightly build.
- Exploration picks start immediately: that is how the first signals arrive without asking.

**Very little data.**
- One signal already moves offers near it, by at most ±0.027 (cap formula). Ten signals give
  ±0.15.
- Reason routing makes one "trop loin" fix the distance factor only. It does not penalise every
  similar job.
- Active questions target the most uncertain offer, so each answer carries the most information.
- Profile suggestions turn a repeated pattern into one explicit, user-confirmed profile change. The
  pattern is ≥ 3 positives sharing an unlisted occupation, skill or keyword, with lift ≥ 2 vs its
  exposure share. One confirmation has more effect than many implicit updates, and it is fully
  transparent.

**Not reinforcing wrong assumptions:**

| Risk | Mechanism |
|---|---|
| Feedback loop (only shown offers get signals) | exposure-aware inverse propensity for implicit signals; skips inferred only at positions ≤ 5; outside lane and exploration expose non-top offers |
| Filter bubble | diversity guard; exploration; prior shrinkage (the profile always pulls back) |
| "Viewed" read as "liked" | view = +0.25, once; an explicit negative overrides it |
| Wrong generalisation of a negative | reason routing; a hidden offer is removed but generalises only through the embedding and occupation, at the same capped rate |
| Stale goals | 90-day half-life; profile-change discount × 0.5 |
| Accidental clicks | `unsaved` / `unhidden` / `unsure` undo; per-offer aggregation counts once |
| The model drifts worse | AD-5 health monitor: cap halving, pause, notice |
| Engine learns one user's bias | impossible by construction: P never feeds E (§2) |
| Evaluation leakage | held-out labels never read by P; the shadow uses future positives only |

---

## 9. Stages and measurable success criteria

Each stage is one or more PRs. A stage is "done" only when its criteria are met, or explicitly
reported as not met (R-Q6).

| Stage | Contents | Exit criteria |
|---|---|---|
| **L0 Decisions** | ADRs 0014+ for approved ADs; amendments to 0001/0002/0009/0011/0012 | merged; no code |
| **L1 Signals** (AD-6, AD-12) | exposure log, Save/Hide, reset/erase/export, socket-guard test | AD-6 criteria; INV-2 test green; erase/reset tested on a temporary DB |
| **L2 Structured evidence + ops base** (AD-7.2 step 1, AD-9 partial) | FT ROME/experience/education/salary into typed evidence; extractor-vs-structured report; source/field detectors | ≥ 99 % FT ROME coverage; first extractor precision/recall published in Quality; detector fault tests pass |
| **L3 Personal model, shadow** (AD-3, AD-5) | Bayesian model computed and journaled, not shown | offline learning-curve criterion; INV-7 bit identity; latency budget; ≥ 14 days of shadow on the owner's install + ≥ 2 testers with no guardrail failure |
| **L4 Personal model on** (AD-4, AD-11 partial) | activation rule; "learned" panel; exploration; health monitor | AD-5 activation reached or 30-day rule; fault-injection pause works; usability: the testers can find and use reset |
| **L5 Weak evidence** (AD-2, AD-11) | pre-registration → lab → per-check rollout (age, then title, then seniority) | AD-2 criteria 1–5 on tuning (exploratory) and real-v3 held-out (decisive) |
| **L6 Occupations** (AD-1, AD-7) | pack, classifier, profile picker, domain basis via the lab | AD-7 criteria incl. the multi-occupation self-check |
| **L7 Market** (AD-8) | aggregates, Skill Graph v2, suggestions, relatedness terms | AD-8 criteria |
| **L8 Full self-monitoring** (AD-9 rest) | duplicates, systematic-filter proposals, classifier drift | AD-9 criteria |

Order: L1 and L2 can run in parallel. Then L3 → L4. L5 starts after its pre-registration (interim
floor) and is upgraded after L6. L6 → L7 → L8. (Sequential execution per the owner's preference:
one at a time, in this order: L1, L2, L3, L4, L5, L6, L7, L8.)

---

## 10. Failure modes, safeguards and rollback (consolidated)

| Failure | Detection | Safeguard | Rollback |
|---|---|---|---|
| Personalization makes results worse | AD-5 win rate, top-20 positive rate | cap(n), shrinkage, diversity | auto-halve → auto-pause; user reset; algorithm flag |
| Learned state corrupt | `state_hash` mismatch | INV-4 | rebuild from the journal |
| Weak-evidence floods the user | lane size, open rate (AD-11) | caps, floors | per-check `reject`; lab rollback |
| Weak check was protective | AD-9 positives on strong-rejected pool, user hides on lane | per-check rollout | per-check flag |
| Occupation misclassification | held-out FT accuracy alert | cap requires confidence ≥ 0.8, never on targets | `domain_basis: keywords` |
| Knowledge pack corrupt or missing | SHA-256 manifest | keyword fallback | previous pack |
| Market figures misleading | n disclosure, k ≥ 5 | non-deciding use only | hide pages |
| Alert fatigue | false alarms per week | min volume, persistence, digest | per-detector off |
| Migration failure | migration ledger | backup first, additive only | restore backup; older version ignores the new tables |
| Performance regression (more offers scored) | run duration in `pipeline_runs` | lazy embeddings for `below_threshold` | flags |
| Privacy leak | socket-guard test, log lint | INV-2 import rules | — |
| Held-out leakage into P | test that P never reads held-out ids | split enforced in the calibration reader | — |

---

## 11. Open parameters (initial values, adjustable only through the lab or releases)

`cap(n) = 0.30·n/(n+10)`; `β = 1`; `α_e = 4`; `κ = 3`; signal weights (AD-3.1 table); implicit
propensity clip [1, 3]; skip rule (position ≤ 5, ≥ 3 sessions); exploration 2 of 20; diversity 6
occupation / 3 company; `outside_lane_size = 10`; weak penalty 0.10 each, 0.25 total; age penalty
ramp 1×–3× `freshness_days`; classifier k = 15, abstain 0.35, cap confidence 0.8; relatedness
thresholds 0.2 / 0.5 / 0.6 / 0.8; AD-5 thresholds (8 discordant, 0.60 / 0.40, 30 days); retention
(exposure 180 d, shadow 90 d, ops 400 d).

---

## 12. Decisions requiring your approval

Approve, amend or reject each one:

| # | Decision | Key consequence if approved |
|---|---|---|
| AD-1 | Shared intelligence only via release knowledge packs; no telemetry in 2.x | no server; engine changes stay developer-validated |
| AD-2 | STRONG / WEAK / UNKNOWN; only STRONG rejects; capped "outside your criteria" lane | contract change to filter semantics; pre-registration + lab before default |
| AD-3 | Bayesian preference model with profile prior; signal table and reason routing as specified; on by default after shadow | ADR 0009 reversal; legacy personalization retired |
| AD-4 | Personal learning updates itself within bounds, outside the lab; global changes unchanged | ADR 0011 amendment; P4/C10 replaced |
| AD-5 | Shadow pairs + discordant-positive evaluation + auto-pause | personal activation without labels |
| AD-6 | Exposure log, Save, Hide, optional active questions (≤ 3/week) | new prunable table; vocabulary bump |
| AD-7 | ROME + ESCO occupation model in a knowledge pack; kNN classifier on stored embeddings; occupation picker in the profile | profile contract change; P3 dependency; new pack file (licences to verify) |
| AD-8 | Market aggregates built before cleanup; Skill Graph v2 on ESCO skills | new tables; history beyond deletion |
| AD-9 | Detectors + alerts + proposals; automatic actions limited to breakers, UNKNOWN-on-disagreement, AD-5 pause | new tables; Quality › Santé |
| AD-10 | Labels = developer instruments; volunteer programme | reframes F0.2/R-Q5 |
| AD-11 | Home sections and lane caps | P6 scope |
| AD-12 | Retention table; in-app reset and erase (ADR 0009 amendment); export of learned data | trigger-drop maintenance path |
| AD-13 | Replace P4 with L1–L8; P3 gains the occupation picker | roadmap update |

**To verify before writing the ADRs** (not yet verified):
- ROME 4.0 and ROME↔ESCO crosswalk availability, licence and the existence of "mobilité" links;
- exact France Travail offer field names for experience and education;
- ESCO licence and pack size.
