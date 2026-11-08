# L3 sequential evaluation — technical correction to the 2026-11-06 research package

Date: 2026-11-06. Repository: `ItackFPS/Find-Your-Job-With-AI-Public` at `origin/main` 94578c6
(detached research worktree; nothing committed). Companion to
`docs/reports/2026-11-06-safe-passive-personalization-research.md` (the "package").

Status: **research only.** No change to code, schema, configuration, activation flags, datasets,
the L5 pre-registration or ADR 0019. L3 stays SHADOW, activation stays OFF. Every amendment below is
a **recommendation** for the owner.

Labels: **Repo** (read in code at 94578c6), **Math** (derivation in this report), **Sim**
(experiment run for this report, §7), **Inference**, **Recommendation**.

---

## 1. Verdict in five lines

1. **The package's proposed test is not valid for FYJ's actual observation process.** Its null
   ("challenger win probability ≤ ½ among discordant positives") is not the no-effect null, even on
   perfectly randomized S4 evidence: the share of offers a challenger moves up vs. down by ≥ 3
   positions is a property of the challenger's adjustment shape, not of the user. A worthless
   challenger with many small promotions and few large demotions wins 72 % (cap 0.3) to 86 %
   (cap 0.6) of discordant S4 positives while being worse for the user (Sim §7.1). The package's
   "LB test is immune to SR-1" was an input of its generator (`p_LB = 0.5` hard-coded), not a finding.
2. **Excluding no-interaction sessions is harmless for paired/within-item contrasts and for
   inverse-probability slot contrasts** (zero units contribute a factor of exactly 1), **but not for
   between-unit comparisons of non-binary outcomes, and not when inclusion or order depends on
   outcomes** (positive-time ordering with outcome-dependent delays, stratum reclassification after
   later PF exposure). The fix is an assignment-defined unit with a fixed outcome window (§3).
3. **S4 identifies randomized item exposure only.** With both rankings logged per run it gives a
   design-unbiased estimate of an *offline, item-additive* ranking-metric difference (ΔDCG on
   neutral-exposure relevance) — valid under arbitrary outcome dependence — but not the effect of
   showing the challenger's list, and at FYJ volumes it has **0 % power within one year** (Sim §7.2).
4. **A fresh α = 0.05 per release, reset or pause is not safe.** Over 16 quarterly releases, lifetime
   false activation reaches 11–28 %; ADR 0019's "resume on a fresh window after a pause" reaches
   42–44 %; carrying one global wealth across releases lets a good release activate later bad ones
   (24 %). α-spending across a pre-registered test sequence holds ≤ 1.5 % (Sim §7.3).
5. **No design simulated here reaches useful power for this user at simulated volumes.** The best
   valid designs — a randomized novel-item slot with a symmetric control (C1b′) and team-draft
   interleaving — activate a strongly better challenger in 20–53 % and 33–36 % of one-year
   histories respectively (false activation ≤ 3.8 % in every null scenario), with a median of
   ~6–7 months when they do. **Conclusion: no suitable activation design is
   currently available.** Keep Option A; a revised Option C (§6) is defined so that, if real
   volumes turn out much higher, it can be adopted without repeating this analysis.

---

## 2. What the code actually observes (Repo)

- **S4 draw** (`src/learning/exploration.py`): before ranking, `draw()` takes up to 2 offers
  uniformly from E = offers that reached scoring, minus hidden offers, minus offers shown in
  Découverte in the last 14 days; seed from `secrets.token_hex`, pool sorted, `random.Random(seed).sample`.
  `s4_draw` stores run, job, slot, π = k/|E|, seed, |E|. Saved/applied offers are **not** excluded
  from E.
- **Both rankings logged:** `ranking_shadow` holds production and challenger positions for every
  ranked offer of the run, including the drawn ones (`record_in_shadow`); drawn offers are removed
  from S1 only at the API (`src/api/routes/jobs.py:301`) for that run, and shown in their own
  section (`/jobs/decouverte`, `frontend/src/pages/Jobs.tsx:478`). Nothing else is randomized:
  there is no assignment between production and challenger anywhere.
- **Evidence unit** (`src/learning/health.py` `record_positives`, `src/learning/gates.py`): a
  positive (aggregated y ≥ +1.0) is anchored once to a run; its stratum is LB only if *every*
  surface it was seen on in the 14 days before the positive is LB (`stratum_of`: any S1/S5/S2-by-
  relevance exposure ⇒ PF); discordant = |Δposition| ≥ 3 with positions capped at 101
  (`POSITION_CEILING`); win = challenger position smaller. Only positives enter: draws without a
  positive leave no row.
- **Restarts** (`health.update_health`): a release-key change returns ACTIVE to SHADOW; after a
  pause "only positives received since the pause count" (fresh window).

---

## 3. Question 1 — outcome-based selection

### 3.1 When excluding no-interaction units is harmless (Math)

Write the per-unit contrast as a bounded `Z_t ∈ [−1, 1]` with `Z_t = 0` whenever the unit has no
qualifying interaction, and the betting e-process `K_t = Π (1 + λ_t Z_t)` with λ_t predictable.
Every no-interaction unit multiplies K by exactly 1. So **the positive-only process is a time change
of the all-units process and has the same validity and the same decisions** — *provided* (a) the
null is stated for the all-units mean, `E[Z_t | F_{t−1}] ≤ 0`, and (b) which units exist and in what
order does not depend on outcomes. Sim check on the package's own generator: the all-sessions
version of its session e-process (zero sessions enter at X = ½) gives identical decisions in every
history (§7.4, "mismatches = 0").

This covers: paired within-item contrasts (S4: `Z = Y·D`), team-draft session credit (`Z = sign(credit_C − credit_P)`,
no positive ⇒ 0), and an inverse-probability single-slot contrast (`Z = Y·(2A − 1)`, π = ½).
For a binary slot outcome with π = ½ the positive-only conditional test ("is A = 1 among positive
slots more than half the time?") is the same test.

### 3.2 When it is not harmless

| Mechanism | Present in FYJ? | Effect |
|---|---|---|
| **Between-unit comparison of a non-binary outcome conditioned on ≥ 1 positive** (e.g. A/B of whole lists, mean positives among positive-bearing sessions) | not today; would arise in any session-level A/B | Post-treatment selection. Example: production 10 % of sessions with 3 positives (mean 0.3), challenger 50 % with 1 (mean 0.5): the conditional means (3 vs 1) reverse the true order. Never use. |
| **Unit order set by outcome arrival** (package §4.2: "the unit enters the sequence when its outcome is first observed") | yes, if implemented as written | Valid only if delay is independent of the contrast. If wins arrive faster than losses (plausible: offers the challenger likes may be saved quickly, others applied to later), the early sequence over-represents wins: the conditional-mean null fails at the start of every process and after every restart. Not detectable at FYJ volumes (Sim §7.1 "delay-skew": no test fires), so it must be prevented by construction. |
| **Stratum reclassification after randomization** | yes (`stratum_of`) | An S4-drawn offer that later appears on S1 before the positive becomes PF and is dropped. Later S1 exposure depends on production rank, which correlates with the win/loss sign (offers production ranks high can only lose or tie). Measured effect small here (89–97 % of discordant S4 positives kept; LB win rate 0.506 vs 0.500 under the symmetric null, Sim §7.1), but it is a post-randomization selection and must not define the unit. |
| **Already-positive offers redrawn** | yes (E keeps saved/applied offers) | No double count today (`shadow_positive` is keyed by job), but such draws can never produce a new positive; they waste slots. Exclude them from E **before** the draw (a pre-assignment rule). |

### 3.3 Comparison of the four outcome definitions

| Definition | Validity of a sequential test | What it measures | Usable now? |
|---|---|---|---|
| All eligible randomized units incl. no-interaction (Z = 0) | valid if units/order are assignment-defined | depends on Z (below) | yes, for S4 |
| Positive-bearing units only | same decisions as above when Z = 0 off-positive and order is assignment-defined; **invalid** under outcome-dependent order/reclassification or between-unit non-binary comparisons | same | only as an implementation shortcut of the above |
| Pairwise preference (paired, e.g. interleaving credit sign) | valid under the team-draft randomization (see §5 caveat) | user preference between the two rankers' contributions in one list | needs interleaving (not built) |
| Bounded session/draw utility (e.g. `Y · Δw`, `Y·(2A−1)`) | valid, design-based (§4.2) | a pre-registered utility contrast | S4 version yes; slot version needs C1b |

### 3.4 Outcome definition that does not select on realised outcomes (Recommendation)

- **Unit** = one randomized assignment event, fixed when it is made: an S4 draw that reached visible
  exposure (ADR 0016 rule; the Découverte section's layout does not depend on which offer was drawn),
  or one randomized session/slot (C1b, interleaving).
- **Pre-assignment eligibility only:** E excludes hidden, recently drawn **and already-positive**
  offers; nothing after the draw (later surfaces, later positives) can remove a unit.
- **Outcome** `Y` = first qualifying positive (y ≥ +1.0) within **14 days of the unit's first visible
  exposure**; `Y = 0` otherwise. The unit enters the sequence **at window close, in assignment
  order**, whatever its outcome. Late positives are ignored by rule.
- **Contrast** `Z` is a pre-registered bounded function of `Y` and pre-assignment quantities only
  (§4.2), never a stratum computed after the draw.

---

## 4. Question 2 — what S4 identifies

### 4.1 The three things are different

| Claim | S4 provides it? | Why (Repo/Math) |
|---|---|---|
| Randomized item exposure | **yes** | uniform draw from E with recorded seed and π |
| Randomized assignment between production and challenger | **no** | nobody is ever shown the challenger's list or a challenger-chosen item; both rankings are only logged |
| Valid paired comparison of the two ranking policies (online effect of showing list C vs list P) | **no** | requires exposure under each policy; S4 observes the user's response to an item in a neutral section, not to a list |

### 4.2 What an S4 observation can estimate (Math)

For every offer i ∈ E of a run, both positions are known before the draw and do not depend on it
(the draw happens first; `ranking_shadow` keeps drawn offers). Let `y_i` be the user's potential
response to i if drawn (positive within the window), and `w(·)` a pre-registered position weight
(e.g. top-20 DCG). Compute positions **within E** (relative order preserved), so
`Σ_{i∈E} [w(c_i) − w(p_i)] = 0` exactly. With a uniform draw,

`E[ Y · (w(c) − w(p)) | past ] = (1/|E|) Σ_{i∈E} y_i (w(c_i) − w(p_i)) = (DCG_C − DCG_P) / |E|`

on relevance `y` measured under neutral exposure. Because the only randomness used is the draw, the
null "at every run, the current challenger is no better than production on the current pool for the
user's current responses" makes `K_t = Π(1 + λ_t Z_t)` a supermartingale **under arbitrary
dependence between outcomes, repeated companies, topic drift and a challenger retrained on past
outcomes** (the potential responses at run t may depend on anything in the past; the draw does not).
Rejection then estimates the *time-averaged* offline ΔDCG, not the current version's.
This corrects the package's claim that "dependence survives randomization": it survives only for a
*superpopulation* win-rate null; a design-based null on the current pool is immune to it.

The alternative, a sign test against the **design null rate** `q0_t` = share of discordant offers in
E that the challenger moves up (`Z = Y·(win − q0_t)` on discordant draws), is valid for "no
association between the challenger's displacement and the user's response", but ignores where in the
list the displacement happens.

Assumptions the S4 estimand adds (cannot be tested from S4 alone): relevance is item-additive (no
list context), the Découverte framing scales responses uniformly across offers, the weight `w`
reflects utility, and no interference between concurrently pending draws.

### 4.3 What the current evidence rule estimates

The LB sign test compares `P(win | discordant positive, LB)` with ½. Under "no association" this
equals `q0`, which ranges 0.15–0.86 in the simulated challengers (Sim §7.1). So the rule tests
whether the challenger's adjustment is skewed toward promotions, mixed with any real signal. It is
neither the online policy effect nor the offline ΔDCG.

### 4.4 Smallest additional experiment that identifies a policy effect

To estimate "is the personal challenger better than the production baseline" as an **online**
effect, some sessions must expose challenger choices under randomization. Smallest options:

1. **C1b′ — randomized novel-item slot with a symmetric control.** One slot (e.g. position 3):
   with π = ½ show the challenger's best offer not in production's top 20 (and not positive, not
   drawn); otherwise production's own best offer not in its top 20. `Z = Y_slot·(2A − 1)`.
   This identifies the causal effect of the *deployable policy* "fill one slot with the challenger's
   pick instead of production's next pick". The control matters: the package's C1b (control =
   production's item at position 3) is valid but compares a fresh offer with an offer the user has
   usually already seen or saved; a worthless challenger then "wins" in 18 % of one-year high-activity
   histories (Sim §7.2) because of repetition, not personalization.
   With the symmetric control it held false activation ≤ 3.6 % and reached 20 % / 53 % power in a
   year for a strongly better challenger (typical / high activity, Sim §7.5).
2. **C2 — team-draft interleaving** of the top 20 in randomized sessions: paired, more units
   (~100–150 a year) but each diluted by list overlap; 33–36 % power in a year, ≤ 3.8 % false
   activation. Changes the whole list, so a larger INV-6 exposure than C1b′.

Both show unvalidated challenger picks before any evidence exists (INV-6 question, owner decision).
S4 cannot be upgraded to identify the policy effect without one of them.

---

## 5. Question 3 — lifetime error budget

### 5.1 How false activation accumulates (Math + Sim)

With independent fresh α per test, `P(≥ 1 false activation) = 1 − Π(1 − α_r) ≤ Σ α_r`; with
dependence the union bound `Σ α_r` still holds and is the only safe planning figure. ADR 0019 and
`health.update_health` start a new test at **every release-key change** (algorithm or any config
digest change), after **Reset**, and after **every pause** (fresh window). Sim §7.3 at boundary null
(p = ½), 16 quarterly releases over 4 years:

| Policy | ~3 decisive units/month | ~10 units/month |
|---|---|---|
| fresh α = 0.05 per release | 11.1 % | 28.3 % |
| fresh α per scheduled 30-day restart | 0.6 % | 33.8 % |
| restart on a fresh window after a data-dependent pause (ADR 0019) | 11.0 % | 44.4 % |
| one global e-process carried across releases (all null) | 4.0 % | 2.9 % |
| global carry-over, release 1 truly better, later null releases | 10.2 % | 24.0 % |
| α-spending α_r = 0.05·6/(π² r²) | 0.5 % | 1.4 % |
| α-spending α_r = 0.05/2^r | 0.5 % | 1.5 % |

Low rates look safe only because tests rarely fire at all. The pause rule is the worst: the restart
happens precisely when the evidence is unfavourable, which discards bad stretches (a classic
optional-restart exploit).

### 5.2 Options compared

| Procedure | Guarantee | Cost | Verdict |
|---|---|---|---|
| Fresh α per release/reset/pause | per test only; lifetime unbounded | — | **reject** |
| Lifetime α-spending over a pre-registered sequence of tests (union bound, e-value ≥ 1/α_r) | lifetime FWER ≤ α, any dependence | later tests need more evidence (threshold 1/α_r: 33, 132, 296 …); power of a 5th-release challenger fell from 26 % to 2–3 % (Sim) | **recommend**, with few tests |
| One global e-process across releases | FWER ≤ α only for "every release null"; rejection says *some* release was better, not the current one | carried wealth activates bad later releases (24 %) | **reject for activation** |
| Combine per-release e-values (average, Vovk & Wang) | valid for the global null | same interpretation problem | descriptive only |
| Online FDR with e-values (e-LOND-type) | bounds the expected share of false activations | FDR is the wrong error for a single user (one false activation is the harm) | not recommended |
| Expected-number control (PFER = Σ α_r) | E[# false activations over life] ≤ Σ α_r | same mechanics as spending | acceptable statement of the same schedule |
| Fixed planned count (R_max tests at α/R_max) | FWER ≤ α | simple; forces release discipline | **recommend** as the simplest spending rule |

### 5.3 How each event should affect the guarantee (Recommendation)

- **Hypothesis identity** = `release_key` (algorithm version + config digest). Continuous retraining
  within a release is part of the challenger; a rejection then means "on average over the release's
  versions the challenger was better", not "the current version is". Freeze hyperparameters within a
  release; any change is a new release.
- **New release:** a new test consumes the next α slot when it *starts*, whether or not it ever
  decides. The previous release's process stops for good.
- **Pause** (health monitor, guardrail, manual): **does not restart** the test. The release's
  e-process keeps its wealth and keeps accruing units (pausing only removes the challenger from what
  the user sees). Re-activation after a pause requires the same process to be at or above its
  threshold again, or consumes a new α slot if a fresh window is wanted.
- **Reset learning:** erases the e-process state with the other derived data (ADR 0015). Either keep
  a content-free counter of α slots spent across resets (owner/privacy decision), or state that the
  guarantee is per reset epoch.
- **Harm monitor:** a separate e-process for "challenger worse"; false pauses are the safe direction
  and need no budget, but they must not reset the activation process.
- **Restart triggers** may depend only on time, release changes and user actions, never on the
  value of the process.

---

## 6. Question 4 / Deliverable 4 — revised Option C, or none

**Conclusion: no suitable activation design is currently available.** At the simulated volumes no
valid design reaches 80 % power within a year (§7.2, §7.5); the most efficient reach 20–53 % (C1b′)
and 33–36 % (interleaving) for a strongly better challenger. Making the existing gate pass is not a goal.

**Revised Option C (for adoption only if real volumes are several times higher than simulated):**

1. **Deployable policy = tested policy.** Define activation as a bounded, specific action (e.g. "the
   challenger may fill slot 3 with its best novel offer" or "interleave"), and test exactly that.
2. **Evidence** only from randomized assignment of that policy (C1b′ with symmetric control, or C2
   interleaving); S4 ΔDCG kept as a **harm/diagnostic** statistic, not activation evidence.
3. **Units and outcome** as §3.4: assignment-defined, fixed 14-day window from first visible
   exposure, entered at window close in assignment order, Z = 0 counted.
4. **Test:** betting e-process (mixture over λ), design-based null on the current assignment;
   threshold `1/α_r` from a pre-registered lifetime schedule (e.g. R_max = 5 tests at α = 0.01 each).
5. **Restart rules** §5.3; no fresh window after a pause.
6. **Simulator gate before any owner decision:** the §7 grid on owner-replay volumes; required:
   false activation ≤ α_r per test and ≤ α lifetime; power ≥ 80 % within 12 months at the smallest
   effect the owner cares about. If it fails, Option C stays unadopted.

Option A (SHADOW, activation OFF) remains the working position; Option D (confirmed suggestions)
is unaffected by this report.

---

## 7. Simulations

All scripts are outside the repository (`FYJ-workspace/scratch/2026-11-06-l3-correction/`); they
import repository constants read-only (`gates.POSITION_CEILING`, `DISCORDANCE`, `p_better`) and the
package's generator unchanged. Every parameter is synthetic.

**Item-level simulator (new, `obs_sim.py`).** 300 live offers, 5 %/day churn, 8 topics, 80
companies (Zipf), daily runs; user utility = drifting topic preference + company preference + item
term; production score partly aligned with the user. Sessions 4/week (typical) or 6/week with
deeper examination (high); position-biased examination of the top 20; S4: 2 uniform draws, 70 %
examined, response scaled 0.4. Delays: geometric (mean 2 d) within 14 d. Calibrated to ~0.9
positives/session, ~2.4 S4 positives/month (the package's LB volume order). Challengers (additive
`cap·tanh(φ)`): **baseline** symmetric noise; **null-skew** many small promotions, few large
demotions; **SR-1** learned production's topic pattern; **SR-2 ρ** session topic focus + company
boost after a positive (ρ = 0.2 / 0.5); **delay-skew** wins arrive in ~1 d, losses ~7 d;
**null-versions** new noise every 60 days + daily AR(1) retraining; **alt-strong / alt-weak**
noisy estimate of the true utility, improving with data. Ground truth = expected positives in the
top 20 under each list (all nulls < 0, alts +0.18 to +0.25).

### 7.1 Observation designs, 180 days, 1,000 histories/cell (activation rate; within 90 d)

| Scenario | activity | G3rep (pooled) | S4half (package) | S4q0 (design null) | S4dcg (design ΔDCG) | nonzero S4 units |
|---|---|---|---|---|---|---|
| baseline | typ / high | 0.3 % / 0.7 % | 0.4 % / 0.9 % | 0 / 0 | 0 / 0 | 5.3 / 7.8 |
| null-skew | typ / high | 0 / 0 | 0 / 0.1 % | 0 / 0 | 0 / 0 | 0.8 / 1.2 |
| SR-1 | typ / high | 0.6 % / 0.9 % | 0.1 % / 2.0 % | 0 / 0 | 0 / 0 | 3.9 / 5.7 |
| SR-2 ρ = 0.2 | typ / high | 0.6 % / 0.4 % | 0.5 % / 0.7 % | 0 / 0 | 0 / 0 | 5.8 / 8.4 |
| SR-2 ρ = 0.5 | typ / high | 1.0 % / 0.3 % | 0.5 % / 0.8 % | 0 / 0 | 0 / 0 | 6.4 / 9.6 |
| delay-skew | typ / high | 0.8 % / 1.0 % | 0.2 % / 0.8 % | 0 / 0 | 0 / 0 | 5.2 / 7.7 |
| null-versions | typ / high | 0.9 % / 0.3 % | 0.1 % / 1.0 % | 0 / 0 | 0 / 0 | 5.2 / 7.6 |
| alt-strong | typ / high | 39.8 % / 44.8 % | 8.9 % / 24.0 % | 0 / 0.5 % | 0 / 0 | 5.4 / 8.0 |
| alt-weak | typ / high | 14.1 % / 20.6 % | 4.4 % / 15.1 % | 0 / 0.1 % | 0 / 0 | 5.7 / 8.4 |

Large-sample null rates (150 histories pooled, typical): discordant S4 positives won 0.500 (baseline,
q0 0.517), **0.719 (null-skew cap 0.3, q0 0.684) and 0.864 (cap 0.6, q0 0.830)**, 0.566 (SR-1,
q0 0.569), while every one of these challengers is worse than production. Alt: 0.822 vs q0 0.577.

Readings: (i) every test has low false activation here **because volumes are tiny**; the package's
test is biased by construction (null rate ≠ ½) and would converge to activating null-skew and SR-1
with more data. (ii) Its apparent power (up to 24 %) partly comes from the same bias (q0 0.58 under
alt). (iii) The valid design-based tests have essentially zero power: 1.6–2.6 nonzero ΔDCG units in
180 days. (iv) G3rep is far safer here than on the package's generator (16–25 %): in this model a
null challenger loses pooled PF comparisons (offers production shows at the top can only lose),
so its behaviour depends entirely on the generator — another reason not to trust pooled evidence.
(v) Delay-skew changes nothing measurable at this volume; it is excluded by construction (§3.4).

### 7.2 Policy-assignment designs (activation rate; nonzero units)

180 days, 500 histories/cell, cap 0.3:

| Scenario | C1b typ / high (control = production's position-3 offer) | TDI typ / high |
|---|---|---|
| baseline | 1.4 % / 6.8 % | 0.6 % / 0.4 % |
| null-skew | 2.0 % / 14.6 % | 1.0 % / 2.4 % |
| SR-1 | 2.6 % / 10.4 % | 2.4 % / 2.0 % |
| SR-2 ρ = 0.5 | 1.6 % / 6.8 % | 1.2 % / 1.0 % |
| null-versions | 4.0 % / 11.4 % | 2.6 % / 3.8 % |
| alt-strong | 7.4 % / 23.2 % | 15.4 % / 18.8 % |
| alt-weak | 4.4 % / 17.4 % | 8.6 % / 7.8 % |
| nonzero units / 180 d | 9–18 | 47–72 |

List overlap (top 20 shared by both lists): 0.93 (cap 0.15), 0.86 (0.3), 0.74–0.80 (0.6). Power of
TDI at typical activity: 10.2 / 15.4 / 20.4 % for alt-strong at cap 0.15 / 0.3 / 0.6; null stays
≤ 0.8 %.

One year (365 days, 500 histories/cell):

| Scenario | S4q0 typ / high | S4dcg | C1b (pos-3 control) typ / high | TDI typ / high | TDI median days to decision |
|---|---|---|---|---|---|
| baseline | 0 / 0.2 % | 0 / 0 | 4.2 % / **18.0 %** | 0.8 % / 0.6 % | — |
| SR-2 ρ = 0.5 | 0.2 % / 0.4 % | 0 / 0 | 7.2 % / 21.6 % | 1.4 % / 1.0 % | — |
| alt-strong | 2.4 % / 17.0 % | 0 / 0 | 27.8 % / 62.2 % | **32.6 % / 35.8 %** | 191 / 169 |
| alt-weak | 2.4 % / 7.4 % | 0 / 0 | 16.4 % / 49.8 % | 19.6 % / 21.4 % | 210 / 219 |
| nonzero units / year | 11–21 | 3.5–6.6 | 19–38 | 98–150 | |

C1b with the position-3 control is a valid test of the wrong comparison: the control offer is the
same, usually already-seen or already-saved offer day after day, so any fresh offer beats it
(18–22 % "false" activation for worthless challengers at high activity). Symmetric novel-item
control (C1b′): §7.5.

### 7.3 Lifetime budget (abstract decisive-unit stream, 1,000 histories, 16 releases × 90 d)

See the table in §5.1. Power for a better release (P(win) = 0.65): release 1 — fresh 28.5 %,
spending 16.7–22.6 % (10 units/month); release 5 — fresh 26.0 %, spending 2.3–3.2 %.

### 7.4 Package experiment re-run (comparability, 1,000 histories, seed 7)

The package's script was re-run unchanged on its generator (20 cells). Every cell reproduces the
package's §4.4 ranges exactly (e.g. baseline G3rep 16.1–25.4 %, e_pooled 2.5–3.1 %; SR-1 e_pooled
22.6–51.3 %; SR-2 ρ = 0.5 e_pooled 7.7–19.8 %; true-0.70 e_LB 1.5–44.2 %). Added column: the LB
session e-process with **every** session included (no-counted-outcome sessions at X = ½) —
identical decisions to the positive-only version in all 20,000 histories (**0 mismatches**), as §3.1
predicts. The package's generator draws "win" as a coin with fixed `p_LB`, so it cannot show the
q0 ≠ ½ problem of §4.3; the item-level simulator is needed for that.

| Scenario (range over 4 cells) | G3rep | e_pooled | e_LB | e_LBsess | e_LBsess (all sessions) |
|---|---|---|---|---|---|
| baseline | 16.1–25.4 % | 2.5–3.1 % | 0.0–2.3 % | 0.0–2.4 % | 0.0–2.4 % |
| SR-1 β = 2 | 67.6–92.8 % | 22.6–51.3 % | 0.0–2.3 % | 0.0–2.4 % | 0.0–2.4 % |
| SR-2 ρ = 0.2 | 21.9–33.0 % | 4.1–7.2 % | 0.1–3.1 % | 0.1–2.3 % | 0.1–2.3 % |
| SR-2 ρ = 0.5 | 27.2–48.6 % | 7.7–19.8 % | 0.1–4.6 % | 0.0–3.7 % | 0.0–3.7 % |
| true 0.70 | 93.0–100 % | 59.7–99.0 % | 1.5–44.2 % | 1.3–43.5 % | 1.3–43.5 % |

### 7.5 C1b′ with symmetric novel-item control (365 days, 500 histories/cell)

Both arms fill slot 3 with a novel offer (not in production's top 20, not drawn, not yet positive):
challenger's best vs production's best.

| Scenario | typical | high | nonzero units / year (typ / high) | median days to decision |
|---|---|---|---|---|
| baseline | 1.6 % | 2.0 % | 31 / 69 | — |
| null-skew | 1.8 % | 2.0 % | 32 / 68 | — |
| SR-1 | 2.2 % | 3.6 % | 31 / 69 | — |
| SR-2 ρ = 0.5 | 0.8 % | 3.2 % | 38 / 85 | — |
| null-versions | 2.2 % | 2.4 % | 32 / 70 | — |
| alt-strong | 20.0 % | **52.8 %** | 39 / 86 | 208 / 186 |
| alt-weak | 9.6 % | 34.8 % | 37 / 82 | 209 / 222 |

The symmetric control removes the staleness artefact (false activation ≤ 3.6 % in every null
scenario, against 18–22 % with the position-3 control) and is the most powerful valid design at high
activity, but stays below 80 % within a year.

### 7.6 Limits

Synthetic user, inventory and click model; volumes calibrated only to the package's placeholders;
truth defined as expected top-20 positives (ignores repeat exposure, which is exactly what C1b's
position-3 control exposes); interleaving credit uses the simulated positives, which are rarer than
clicks; 500–1,000 histories/cell (Monte Carlo SE ≈ 0.7–1 pp near 5 %, ≈ 2 pp near 30 %); common
seeds across cells. Low activity not run (it can only lower power).

---

## 8. Proposed amendments (recommendations only — not applied)

### 8.1 To the research package

1. §1 item 1 and §4.3–4.4: replace "a valid test restricted to randomized evidence fixes SR-1" with:
   a win-rate test against ½ is not valid even on randomized evidence, because the null win rate is
   set by the challenger's adjustment shape (q0 0.15–0.86 simulated); only a design-based contrast
   (`Y·Δw` within E, or a centred `Y·(win − q0)`) is valid. Mark §4.4's e_LB columns "biased null;
   low false activation is zero power".
2. §4.2 table row "Delayed feedback": replace "enters when its outcome is first observed" with
   "enters at the close of a fixed window from first visible exposure, in assignment order".
   Row "Within/Cross-session dependence": add that a design-based null on the current pool is valid
   under arbitrary dependence; the cluster-unit fix is needed only for superpopulation nulls.
   Row "Model retraining" and "Pause/reset": replace by §5.3.
3. §7 Option C: replace C1/C1b by §6; state that S4 identifies item exposure and an offline ΔDCG
   only; add C1b′'s symmetric control; record that no design reaches useful power at simulated
   volumes.
4. §8.2 instrumentation: add (a) exclude already-positive offers from E before the draw;
   (b) store the per-run pool positions needed for `Δw` within E (already derivable from
   `ranking_shadow` + `s4_draw`; confirm the pool equals the ranked set); (c) per-unit window-close
   timestamp.
5. §10: add "lifetime error budget" and "repeat exposure / staleness in slot designs" as resolved
   questions with the answers above.

### 8.2 To ADR 0019 (supersedes the package's §11 where they conflict)

1. **Estimand first.** "Better" means the causal effect of the specific policy that activation would
   deploy, measured under randomized assignment of that policy. Shadow agreement (pooled or LB) is
   descriptive only.
2. **G3/G3b replaced** by a design-based anytime-valid e-process on assignment-defined units (§3.4),
   with the current rule still computed and reported as a comparator (owner decision 2026-11-06).
   This overrides the "never G3" clause; α is not raised for any single test.
3. **No win-rate-versus-½ statistic** is ever activation evidence.
4. **S4** is randomized item exposure; its ΔDCG contrast is a diagnostic/harm statistic, not
   activation evidence. Exclude already-positive offers from its pool.
5. **Lifetime error budget:** a pre-registered schedule of α slots (e.g. R_max = 5 tests at
   α = 0.01, or α_r = 0.05·6/(π² r²)); each release-key test, each re-activation after a pause on a
   fresh window, and each post-reset test consumes one slot. Pauses never reset the activation
   process. Restart triggers never depend on the process value.
6. **Within a release**, hyperparameters are frozen; continuous retraining is part of the
   challenger and the claim is "better on average over the release".
7. **Simulator criteria** extended: per-test and lifetime false activation; power within 12 months;
   scenarios = §7 grid (zero-interaction sessions, repeat exposure, topic correlation, delays,
   versions, releases, overlap, exposure vs assignment) plus the existing baseline/SR-1/SR-2 for
   comparability.
8. **Accepted consequence restated:** with current volumes no activation design meets the criteria;
   ranking personalization stays off (SHADOW) until owner replay shows volumes that would.

Decisions requiring owner approval: all of §8.1–8.2, any interleaving or slot experiment (INV-6),
and the reset-counter privacy question (§5.3).
