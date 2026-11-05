# Find Your Job With AI: global audit of autonomous intelligence and continuous learning

Date: 2026-11-05. Investigation only: no code, configuration or data was changed.

**What was inspected.** Public repository `ItackFPS/Find-Your-Job-With-AI-Public`, `main` at
`6207a7c`, extracted read-only (about 55,500 lines of Python in `src/`, 239 test files). Also read:
the ADRs 0001–0013, `docs/architecture.md`, the 2.0 plan, the status record and the 2026-10-30
completion plan, and the owner's labelling results in `<owner labelling folder>/data/lab/decision-2026-11-03/`
(`REPORT.md` and `FILTER-INVESTIGATION.md`). The private repository (`21eaeeb`, 2026-10-15) is older
and was used only for history.

**Limits.** I ran no test suite and no live database query. Numbers come from code, committed reports
and the owner's labelling reports. External research is summarised from search results; claims marked
*unverified* were not checked against primary documentation.

---

## 0. Executive summary: five findings that change the direction

1. **The quality bottleneck is the hard filters, not the ranking.** In the owner's 100 blind labels
   (real-v2, 11 relevant), the gates reject **3 of the 4 relevant held-out offers**. In the tuning
   part, **5 of the 7 relevant offers** fail the hard filter: 3 on `published_age` (32–57 days old,
   limit 30) and 2 on `title_keywords` ("Développeur Full Stack", "Support applicatif"). Meanwhile
   13 of the 60 non-relevant offers pass. The pre-registered H1/H2 ranking candidates scored
   ΔAUC 0.0 [0.0, 0.0], because a weight change can only reorder the 5 offers the gates let through.
   real-v1 showed the same pattern at scale: the largest rejection reasons in the 20,085-offer pool
   were `title_keywords` (7,041) and `seniority` (5,368). **No learning mechanism placed after the
   gates can recover what the gates have removed.** The first step towards a smarter application is
   to turn keyword and freshness rules from *rejections* into *evidence that ranks*.

2. **Improvement that waits for labels cannot work for a public application, and it barely works
   for the owner.** With about 11 % relevant offers, the pre-registered rule needs 30 relevant
   held-out labels. At a 30 % held-out split, that is about 900 labels from one person. The owner,
   the most motivated user possible, produced 100. Labels should stay a **developer-side
   instrument** (regression safety, release validation). They should not be how quality improves for
   a user.

3. **"Shared intelligence" has no channel today.** The application is local-first, with one user per
   install and no server. Nothing an install learns ever reaches another install. Shared improvement
   can only arrive in two ways: (a) **knowledge the developer ships with each release** (vocabularies,
   taxonomies, models and weights validated on developer benchmarks), or (b) a **new, opt-in,
   aggregate-only telemetry channel**. (b) needs a server, a privacy policy and GDPR work. This is
   the most important architectural decision in this report (D1).

4. **The matching vocabulary is hand-written and centred on IT, which does not suit a public
   product.** Domain relevance (`scoring.py:_score_domain_relevance`) and the title filter work on
   curated keyword lists in `config/filters.yaml`. Software, data, networking, systems and similar
   domains are detailed; healthcare, sales, cleaning and similar domains appear mainly as
   *exclusions*. A nurse, an accountant or a logistics worker gets keyword matching nobody tuned for
   them. Yet the structured occupation data is **already collected and then ignored**: France Travail
   offers carry a ROME occupation code and structured experience, education and salary fields. They
   sit in `jobs.raw_payload` and nothing reads them (only `competences` is used,
   `france_travail.py:883`). The bundled ESCO taxonomy is used only for skill synonyms.

5. **The learning machinery exists, but it is disconnected.** The application already journals every
   decision (`job_decisions`, including rejected offers with text snapshots), every explicit and
   behavioural signal (`feedback_events`: viewed, apply link opened, interested, not interested,
   unsure, mis-ranked, application status), provenance, expiry and a per-query yield. But:
   - explicit feedback does not change the ranking by default (`personalization_bridge: false`);
   - behaviour signals are never used;
   - personalization is token-based (company, title words, skills, remote), not embedding-based;
   - the semantic score carries only **6 %** of the final score;
   - the disagreement engine runs in shadow only;
   - the skill graph is read-only analytics.

   The cheapest large gain is to **connect what is already recorded**.

**Recommended direction in one sentence.** Keep the deterministic, journaled, safety-first core.
Move from "reject on weak evidence, then rank" to "reject only on strong evidence, rank everything
else". Add a personal preference model that learns from normal use, starting from the profile and
never needing a minimum number of labels, and evaluate it in shadow against the user's own later
behaviour. Make operational learning (sources, extraction, deduplication, freshness) automatic and
self-checking. Treat shared intelligence as developer-validated releases, with optional aggregate
telemetry later.

---

## 1. Audit of current capabilities

For each component: what it does, what it produces, what it throws away, and what could be automated
or reused.

| Component | What it does today | Produces / stores | Information discarded or underused | Autonomy / learning opportunity |
|---|---|---|---|---|
| **Discovery / query expansion** (`discovery/query_expansion.py`, `adaptive.py`) | Hardcoded baseline plus profile- or LLM-derived queries, capped at 12; adaptive *reordering* by historical yield | `per_query_stats` in `pipeline_runs.stats_json` | Yield = offers *collected*, not offers *relevant or acted on*; per-query attribution exists only for France Travail | Rank queries by **downstream value** (accepted, opened, saved per query); propose new queries from the titles of offers the user engaged with; retire dead queries automatically |
| **Collection** (15 collectors, LinkedIn and HelloWork scanners, source contract #58) | Polite collection: robots.txt, breakers, cooldown, canary, consent | Jobs + `raw_payload`; `job_provenance`; breaker state | **France Travail structured fields** (ROME code and label, experience required, education, salary, qualification, sector) stored but unused; source-level quality never measured | Per-source quality statistics (completeness, duplicate share, relevant share, expiry speed); automatic schedule tuning from the hours when new offers appear; canary-driven parser-break alerts (exists) |
| **Normalization / validation** | Maps fields onto `Job`; checks required fields | `Job` | Structured codes flattened into text | Map ROME/ESCO occupation, normalized title, structured requirements into typed fields |
| **Deduplication** (`pipeline/deduplicate.py`) | Exact identity: URL, (source, id), and (company, title, location, contract) only when the company is known | Merged provenance | Near-duplicates (reposts, agency copies, slightly different titles) are not detected; anonymous offers are never merged | Embedding plus MinHash near-duplicate *candidates*, merged only above a strict threshold or shown as "probably the same offer"; repost detection feeds freshness (a 40-day-old offer reposted yesterday is alive) |
| **Hard filters** (`pipeline/filter.py`) | Ordered rules: location, remote, work_mode, seniority, published_age, salary, title_keywords, contract; the first failure rejects | `job_rejections`, decision journal | **Most relevant-offer losses happen here** (finding 1). Keyword lists cannot generalise | Split into **strong constraints** (explicit user choices: contract type, max distance, remote-only) and **weak evidence** (title words, age, seniority words), which become ranking features, plus an "outside your criteria" lane |
| **Requirements gate** (`requirements/gate.py`) | Education, experience and principal/mandatory skill hard or soft rejections; ADR 0004 safety work | Typed evidence (SATISFIED / VIOLATED / UNKNOWN) | FT's structured `experience`/`formation` fields are not compared with the text extractor | **Free self-evaluation**: where FT gives structured requirements, measure the extractor against them automatically (precision and recall of years and levels) without any human |
| **LLM eligibility gate** (`ai/eligibility.py`) | Optional; fail-open | Cached verdicts | — | Keep optional; use as an offline *auditor* of gate decisions on samples, not as a production gate |
| **Scoring** (`pipeline/scoring.py`, 1,700 lines) | 5 weighted factors + domain and seniority gates; tiers | `job_scores`, breakdowns | Domain relevance is keyword-based; `skills_match` uses ESCO synonyms only for skills | Occupation similarity (ROME/ESCO + embeddings) as the domain factor; weights estimated from developer benchmarks per release |
| **Semantic matching** (`pipeline/semantic_matching.py`, ONNX MiniLM) | Profile-to-job cosine, rescaled, blended at **w = 0.06** | `job_embeddings` cached per job | Embeddings of every job are stored but used for one cosine only | Reuse for: personal preference vector, near-duplicates, "more like this", clustering the market, exploration |
| **Personalization** (`personalization/engine.py`) | Weighted averages over company / skill / title token / remote from `job_feedback`; bounded ±0.3; re-capped at gates | Cache | Fed only by the opt-in bridge; behaviour unused; no notion of *exposure* | Replace or augment with an embedding-space preference model with shrinkage to the profile (§5.2) |
| **Decision journal** (ADR 0008) | Every job, every run, accept and reject, with evidence, strategy version and exposure rank/tier; snapshots of rejected text | `job_decisions`, `job_snapshots`, `strategy_versions` | Exposure = pipeline rank, not what the UI actually displayed | Basis for counterfactual evaluation; **re-score rejected snapshots when the profile changes** (find offers the old profile missed) |
| **Feedback journal** (ADR 0009) | Append-only explicit, behaviour and outcome events linked to the decision | `feedback_events` | Only `viewed` and `apply_link_opened` as behaviour; no impressions, no list-level hide/save, no dwell | Add list impressions and quick actions; derive signal weights at analysis time (already the ADR's intent) |
| **Disagreement / abstention** (ADR 0010) | Shadow comparison of signals, plus user stance → `review` | Journaled assessment | Never surfaced; uncalibrated | Feed a "Needs your judgement" lane (planned P6) and use user responses to calibrate it, without asking for labels |
| **Evaluation** (`src/evaluation/`) | Datasets, blind calibration in the UI, held-out split, ranking metrics with CIs, sampling cohorts, self-check | Reports, Quality page | Needs labels; one person cannot produce enough | Keep as the developer and release gate; add **label-free online metrics** from the user's own behaviour |
| **Lab** (ADR 0011) | Isolated, human-approved promotion of config bundles; LLM proposer | `lab.db`, bundles | Promotion needs the CLI and label evidence | Keep for *global* strategy changes; do not use it as the path for *personal* adaptation (§5.4) |
| **Skill graph** (`skillgraph/graph.py`) | Read-only demand, co-occurrence and gap analytics over stored jobs | API + Skills page | Relevance proxy = score ≥ 0.65 or acted on | Market knowledge per occupation and region; suggestions to the profile ("offers like the ones you open often ask for X") |
| **AI runtime** (`ai/runtime/*`, catalogue, benchmark) | Detect, install and benchmark local models; Phase 2 enrichment of the top 100 | `ai_analysis` | Enrichment feeds only the 6 % semantic score | Use the LLM sparingly: offer analysis on demand (P5), batch extraction audits, query proposals |
| **Scheduler** | Daily pipeline, scans, catch-up, retries | Executions | — | Adaptive timing per source; automatic re-scoring when the profile changes |
| **Settings** | Sources, background, data | `settings.json` | Many quality knobs live only in YAML (weights, freshness, keywords) | Replace knobs with learned or derived values plus a few understandable choices |

**What is already good and should be preserved.**
- Deterministic gates before optional AI (ADR 0001).
- The stored deterministic score (ADR 0002).
- The append-only decision and feedback journals.
- The shadow-first habit (ADR 0010).
- Pre-registration and the held-out discipline.
- Lab isolation, and human promotion of global changes.
- Local ONNX embeddings.
- Provenance and expiry.
- The source politeness contract.

These are exactly the foundations a safe learning system needs. Most of the work below *uses* them
rather than replacing them.

---

## 2. Assessment of the existing feedback and evaluation systems

**Feedback ("Votre avis").** `interested`, `not_interested` (with reasons such as too far, too
senior, off-domain, salary…), `ranking_feedback`/`mis_ranked`, and `unsure`. It is on the job detail
page only. It is well designed as a *journal*: immutable, linked to the decision and strategy, with
reasons. Its weaknesses as a *learning* source:

- **It has no effect by default.** The bridge to personalization is off, so a user who gives
  feedback sees nothing change. That kills the motivation to give more.
- **The cost to the user is high.** The user must open an offer and pick an answer. The most
  informative moment, scanning the list, has no quick action.
- **Selection bias.** Only exposed offers get feedback; ADR 0009 acknowledges this, and the exposure
  rank is recorded. That is enough for simple propensity correction later.
- **Reasons are under-used.** `too_far`, `salary` and `contract_type` say which *constraint* was
  wrong. They could adjust that constraint's evidence weight, or propose a profile edit, instead of
  only moving similar jobs.

**Behaviour.** `viewed` and `apply_link_opened` are recorded and then never used. Application status
(to_apply → applied → interview → offer) is the strongest signal the application has. It is journaled
as an outcome, and the disagreement engine reads it, but ranking does not.

**Evaluation.** Technically strong: CIs, held-out split, cohorts, pre-registration, blind labelling
in the app. But it is built around labels, and finding 2 shows the arithmetic does not work for one
person. Where it provides real value:

1. **Regression safety for the developer**: the synthetic set + self-check, and "0 new unsafe
   rejections".
2. **Release validation of global changes** on developer-owned benchmarks, ideally built from
   several volunteers' blind labels, not one owner's.
3. **Calibration of the disagreement engine**: a few labels go a long way when they are spent on
   *uncertain* offers (active learning), not on random ones.

It should **never be a prerequisite** for a user's quality, and should never be shown to users as a
task.

---

## 3. Explicit feedback vs autonomous learning

| | Explicit feedback / labels | Implicit and autonomous signals |
|---|---|---|
| Examples | Votre avis, calibration labels, mis-ranked | Opens, apply clicks, saves, application status, time to open, ignored after N exposures, re-search, profile edits, source and extraction agreement |
| Signal quality per event | High, with stated reasons | Noisy; must be weighted (apply ≫ open) and corrected for exposure |
| Volume | Very low (finding 2) | Comes with normal use, at no cost to the user |
| Cold start | None until the user acts | Profile prior + market knowledge from day 1 |
| Bias | Selection (shown offers only), fatigue | Position and exposure bias; clickbait titles; "opened but disliked" |
| Privacy | Local | Local, if computed on the device |
| Best use | Correcting *constraints*; developer validation; calibrating uncertainty | Ranking personalization; source and query value; online evaluation of candidates |
| Compute | Trivial | Trivial for the recommended models (vector maths on stored embeddings) |

**Conclusion.** Both are needed, in different roles:
- Implicit signals drive **personal ranking and operational learning**.
- Explicit feedback is an **optional accelerator and correction channel** with immediate visible
  effect: "OK, fewer offers like this; we'll also stop hiding offers older than 30 days."
- Labelled datasets are **developer instruments**.

The one change in principle: explicit feedback should affect the user's ranking **by default**,
bounded and reversible. Today it does not (D3).

**Avoiding minimum label counts.** Use a Bayesian-style prior: with zero signals the ranking equals
today's deterministic + semantic ranking. Each signal moves it a little. The step size shrinks as
evidence grows and is capped. There is no threshold, just gradually more influence.

---

## 4. External research: what is real and what transfers

| Example | What it really is | What transfers | What does not |
|---|---|---|---|
| **Hermes Agent** (Nous Research, MIT, Feb 2026) | An LLM agent with a closed learning loop: agent-curated memory, autonomous creation and refinement of Markdown "skills", FTS5 recall across sessions, user modelling | The idea of **procedural memory**: record what worked as reusable, inspectable artefacts, refine them with use, keep them as text a human can audit. Maps onto the lab's memory and on per-user "learned rules" shown in plain words | Self-modifying agents in a ranking pipeline. Performance claims (e.g. "~40 % faster research", top of OpenRouter rankings) come from community or secondary sources: marketing-grade. Nous's own docs note the loop may rarely fire in long chats |
| **LinkedIn GLMix** (KDD 2016; JobSearch SIGIR 2017) | Global model + per-user + per-job random-effect coefficients; reported +20–30 % application lift on LinkedIn jobs | **Global + personal decomposition**: one shared model, plus a small per-user correction shrunk towards zero. Exactly the layer split you want | It needs millions of users to fit; at our scale the "global" part is developer-shipped and the personal part is a tiny local model |
| **LinkedIn dismiss / engagement models** (patent US 11194877) | Separate models for positive engagement (apply, save) and dismiss | Treat **negative signals as their own model**, not just negative weight; a quick "hide" action is valuable | — |
| **RecSys Challenge 2017 (XING)** | Job recommendation under cold start; top teams were content-based or hybrid, with gradient-boosted trees; careful negative sampling beat "deleted or unclicked = negative" | For a new user and new offers, **content features dominate**; do not treat unclicked as negative | Collaborative filtering across users: impossible locally |
| **Unbiased learning to rank** (Joachims et al. 2017; Intervention Harvesting) | Correct position bias with inverse-propensity weighting; estimate propensity from logs | Our journal already stores exposure rank, so simple IPS correction is possible when evaluating a candidate ranking on logged behaviour | Full ULTR training needs much more traffic than one user produces |
| **Contextual Thompson sampling / Bayesian linear model on frozen embeddings** | Per-user Gaussian posterior over a preference vector in a fixed embedding space; reported to work well with few interactions (*course project, preliminary*) | **The recommended personal model**: frozen MiniLM embeddings (already stored) + Bayesian linear preference, prior = profile embedding; the posterior variance also drives a small exploration lane | Neural fine-tuning per user |
| **JobBERT v2 / ConTeXT** (TechWolf, 2025) | Open encoders for job-title normalization to ESCO and for skill extraction | Candidates for domain-agnostic occupation matching; **licence and size to check (unverified)**; must be benchmarked against MiniLM on our data | Do not add before the ROME/ESCO structured route is exhausted |
| **France Travail API + ROME** | Offers carry ROME codes, structured competences (with requirement level), education and experience fields (*field names partly unverified*); ROME 4.0 publishes occupation sheets, alternative titles and skills | **Free, official, domain-agnostic occupation evidence** for France, the primary market | Partner offers via other sources lack ROME: an embedding classifier trained on FT's own labelled offers can assign it (self-supervised, see §6) |
| **Mozilla federated ranking (2019) / Prio telemetry** | Federated learning replaced a hand-written URL-bar heuristic (users typed about half a character less); Prio aggregates telemetry privately | Proof that privacy-preserving *aggregate* learning is feasible; Prio-style "counts only" telemetry is the right shape if D1 opens a shared channel | Federated *model training* is overkill at our scale and would need serious infrastructure |

General lesson: the systems that improve "by themselves" in production are not autonomous AI. They
are **logged signals, simple models, shadow or online evaluation and guarded rollout**. The
architecture below follows that pattern.

Sources: [Hermes Agent docs](https://hermes-agent.nousresearch.com/docs) ·
[Inside Hermes Agent](https://mranand.substack.com/p/inside-hermes-agent-how-a-self-improving) ·
[Dionysius / LinkedIn](https://arxiv.org/pdf/1706.03849) ·
[GLMix incremental learning](https://arxiv.org/pdf/2108.13299) ·
[LinkedIn JobSearch SIGIR 2017](https://microsoft.com/en-us/research/wp-content/uploads/2017/07/JobSearch_SIGIR.pdf) ·
[US patent 11194877](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/11194877) ·
[RecSys Challenge 2017](https://recsys.acm.org/recsys17/challenge/) ·
[Practical lessons for job recs in cold start](https://www.microsoft.com/en-us/research/?p=601041) ·
[Unbiased LTR with biased feedback](https://arxiv.org/pdf/1608.04468) ·
[InfoRank](https://arxiv.org/html/2401.12553v1) ·
[CS224R cold-start Thompson sampling](https://cs224r.stanford.edu/projects/pdfs/Ela%20Naz%20Sigin%20Anastasiya%20Masalava%20Eva%20Casto%20submission_416288591/CS224R_Final_Paper.pdf) ·
[Contextual TS, conversational cold start](https://arxiv.org/pdf/2005.12979) ·
[JobBERT v2 / ConTeXT](https://arxiv.org/pdf/2505.24640) ·
[France Travail ROME](https://www.francetravail.fr/employeur/vos-recrutements/le-rome-et-les-fiches-metiers.html) ·
[FT offers dataset](https://www.data.gouv.fr/fr/datasets/offres-demploi-diffusees-a-france-travail) ·
[Mozilla federated ranking](https://arxiv.org/pdf/1911.11807) ·
[Prio slides](https://rwc.iacr.org/2020/slides/Gibbs.pdf)

---

## 5. Proposed architecture for continuous improvement

### 5.1 Four things to improve, kept apart

| Objective | Question it answers | Learns from | Lives where | Who approves a change |
|---|---|---|---|---|
| **A. Personal ranking** (Layer 1) | What does *this* user want? | This user's profile, behaviour, feedback, status | This install only | Automatic, bounded, reversible, shadow-checked; visible and undoable by the user |
| **B. Matching engine** (Layer 2) | What makes an offer a good match *in general*? | Developer benchmarks (volunteer blind labels), synthetic sets, optionally opt-in aggregates | Shipped in releases | Developer, via the lab + held-out evaluation |
| **C. Market knowledge** (Layer 3a) | What does the job market look like? | Every collected offer (all users' installs see similar public offers) | Locally computed; vocabularies and taxonomies shipped | Automatic (descriptive statistics), never decides acceptance |
| **D. Data and decision quality** (Layer 3b) | Is the pipeline doing its job? | Self-consistency checks: structured vs extracted, duplicates, canaries, expiry, source yield | Locally computed; fixes shipped | Automatic detection; code fixes through normal PRs |

The key separation rules:
- Personal preferences adjust **ranking only**, never safety gates (keeps ADR 0001/0003).
- Nothing personal leaves the install unless D1 opens an opt-in aggregate channel.
- Market and quality statistics never change a user's acceptance decisions directly. They feed
  evidence, suggestions and developer work.

### 5.2 Personal intelligence (Layer 1): the preference model

- **Representation.** Each offer already has a 384-dimension embedding plus structured features
  (occupation, distance, contract, seniority, salary, source, age).
- **Prior.** Profile embedding + the current deterministic score. With zero signals, the ranking is
  identical to today's.
- **Update.** A Bayesian linear preference vector over the embedding (plus a few structured
  features), updated from weighted signals: apply/status ≫ save ≫ explicit interested > open; explicit
  not-interested / hide < 0; "shown many times at the top, never opened" is weakly negative and
  corrected for exposure rank. Reasons map to a specific feature (`too_far` → the distance feature,
  `salary` → salary), not to the whole vector.
- **Shrinkage and caps.** Influence grows with evidence and is bounded (like today's
  `max_abs_adjustment`). Recency decay already exists (90-day half-life).
- **Exploration.** A small, labelled "Worth a look" lane (for example 2 of 20) picks offers with
  high posterior uncertainty, including the best offers that only failed *weak* filters. That is how
  the model learns things the profile never said.
- **Transparency.** "Learned from your activity: you open offers with 'support applicatif' → shown
  higher. [Undo] [Add to my profile]". Profile suggestions turn implicit learning into explicit,
  user-confirmed profile changes, the safest kind of learning.
- **Cost.** Microseconds per offer, no LLM, no training run, works on any hardware.

### 5.3 Turning hard rejections into evidence

Rejection becomes a three-level classification:
- **Strong constraint** (explicit user choice, unambiguous data) → reject. Examples: contract type the
  user excluded; distance beyond the user's maximum with a resolved place; remote-only offers
  restricted to another country.
- **Weak evidence** → ranking feature + reason shown. Examples: title lacks role words; published
  31–60 days ago; seniority word in the title; inferred education above the profile.
- **Unknown** → neutral.

Offers that pass only because weak evidence no longer rejects them go to an "Outside your criteria"
lane, ranked by the personal model. This is directly backed by the owner's labels (finding 1), and it
is also what the 2.0 "never reject without strong evidence" product definition already says.
**Requires pre-registration and evaluation on real-v2 tuning and real-v3** before it becomes the
default (D2).

### 5.4 Evaluation without labels: shadow champion / challenger

1. Every run computes the **production ranking** and, in shadow, **candidate rankings** (new personal
   model version, new weights). Both are journaled; only production is shown.
2. Over the following days, the user's actions on offers both rankings saw are attributed:
   - Did the offers the user applied to or saved sit higher in the challenger?
   - Exposure-corrected (inverse-propensity) metrics avoid simply rewarding production for being
     the one shown.
3. Promotion rules:
   - **Personal model updates**: automatic when the challenger beats or matches production on the
     user's own signals over a minimum window *and* every safety guardrail holds (0 new strong
     rejections, gates re-applied). Reversible with one click; logged as a strategy version.
   - **Global strategy changes**: unchanged. Lab + held-out developer benchmarks + human approval
     (ADR 0011).
4. **Health monitor.** If personal-model-driven metrics drop (the user stops opening top offers),
   personalization reverts to the prior automatically and says so. This is the "never silently
   degrade" guarantee.

### 5.5 Operational intelligence (Layer 3b): self-supervised checks

These need no human and no LLM:

- **Extractor vs structured truth.** France Travail's structured experience and education fields
  give automatic precision and recall for the text extractor every day. Disagreements are a ready
  list of extractor bugs (offline, developer-facing) and an UNKNOWN signal in production.
- **ROME classifier for offers without codes.** Train a nearest-centroid or logistic classifier on
  FT offers (which carry ROME codes) over stored embeddings; apply it to EURES, Indeed and the remote
  boards. Accuracy is measured on held-out FT offers automatically. This gives every offer an
  occupation and makes matching domain-agnostic.
- **Source quality.** Per source: field completeness, duplicate share, expiry speed, share of offers
  passing gates, share the user engaged with. Shown in Settings › Sources and used to order or pace
  collection.
- **Near-duplicates and reposts.** Embedding similarity + company/place → candidate pairs; strict
  automatic merge, or a "probably the same" link. A repost refreshes freshness.
- **Drift and breakage.** Canary (exists) + sudden drops in field completeness or volume → alert in
  Settings (exists partly via breakers).
- **Re-scoring the past.** When the profile changes, re-evaluate stored rejected snapshots (the
  journal keeps the text) and surface "N offers you would now match".

### 5.6 Market knowledge (Layer 3a)

Extend the skill graph from IT technologies to ROME/ESCO occupations and skills: demand per
occupation × region over time, typical required experience and education, salary distribution,
co-required skills, typical time to expiry. User-facing as **insights and profile suggestions**,
never as acceptance rules.

### 5.7 Shared intelligence (Layer 2)

- **Now (no new channel).** The developer improves the global engine from (a) synthetic and
  regression sets, (b) blind labels from several volunteers (better than one owner: different
  occupations), (c) self-supervised statistics from developer-side collection. These ship in releases
  as vocabularies, occupation mappings, weights and models, through the lab.
- **Later (D1, opt-in).** Aggregate-only, Prio-like telemetry: counts and metric summaries, e.g.
  "share of applied offers that the weak title filter would have rejected", extractor-vs-structured
  disagreement rates, source health. No offer text, no profile, no identifiers. This needs a server,
  a privacy policy, consent UI and GDPR assessment. Federated model training is not recommended at
  this scale.

### 5.8 Data flow

```text
collect → normalize (+ROME/structured fields) → dedup (+near-dup) → STRONG constraints only
      → evidence (weak filters, requirements, occupation, distance, age) → deterministic score  [stored, ADR 0002]
      → semantic + personal preference model (prior = profile)  → production ranking
                                            ↘ shadow challengers → journal
user actions (impressions, open, save, hide, apply, status, Votre avis) → feedback_events
      → signal derivation (analysis-time weights, exposure correction)
      → personal model update  +  shadow evaluation  →  bounded auto-promotion / auto-revert
self-checks (extractor vs FT structured, ROME classifier, source stats, dup, canary) → Quality / Settings / developer reports
developer: benchmarks + lab + held-out → release (global engine, vocabularies, models)
```

---

## 6. Automation and intelligence opportunities across the application

Rated **impact / effort** (H/M/L).

| # | Opportunity | Layer | Impact | Effort | Evidence / notes |
|---|---|---|---|---|---|
| O1 | Weak filters (title words, age, seniority words) → ranking evidence + "Outside your criteria" lane | 1/3 | **H** | M | Owner labels: 3/4 held-out relevant offers rejected by gates |
| O2 | Use FT structured fields (ROME, experience, education, salary) as typed evidence | 3b | **H** | L–M | Already in `raw_payload`, unused |
| O3 | Extractor vs FT structured: automatic daily accuracy report | 3b | M | L | No human needed |
| O4 | Embedding preference model with profile prior (replaces or extends token personalization) | 1 | **H** | M | Embeddings already stored |
| O5 | Explicit feedback affects the ranking by default, with visible effect and undo | 1 | M–H | L | Bridge exists; turn on + UI |
| O6 | List-level quick actions (save, hide) + impression logging | 1 | M | L–M | Cheap, high-volume signals |
| O7 | Application status as the strongest positive signal | 1 | M | L | Already journaled |
| O8 | Shadow champion/challenger on the user's own behaviour, auto-revert | 1 | **H** (safety) | M | Uses the decision journal + exposure |
| O9 | Profile suggestions from behaviour (user confirms) | 1 | M | M | Safest form of learning |
| O10 | ROME classifier for non-FT offers (self-supervised on FT) | 3b/2 | **H** for a public app | M | Domain-agnostic matching |
| O11 | Occupation-based domain relevance replacing IT keyword lists | 2 | **H** for a public app | M–H | Needs O2 + O10 |
| O12 | Query value = downstream engagement, auto-propose and retire queries | 3a | M | L–M | `adaptive.py` exists |
| O13 | Source quality statistics in Settings; adaptive pacing | 3b | M | L–M | Breakers and provenance exist |
| O14 | Near-duplicate and repost detection | 3b | M | M | Embeddings exist |
| O15 | Re-score rejected snapshots after a profile change | 1 | M | L | Snapshots exist |
| O16 | Raise the semantic weight from 0.06 via benchmark, or let the personal model own it | 2 | M | L | Today embeddings barely count |
| O17 | Disagreement engine → "Needs your judgement" lane; calibrate from responses | 1/3 | M | M | Shadow today |
| O18 | Active learning: when the user does give feedback, prefer to *ask* about uncertain offers | 1 | M | L | Better than random labels |
| O19 | Market insights per occupation and region (skill graph v2) | 3a | M | M | Read-only today |
| O20 | LLM as an offline auditor of samples (developer side), never as a production gate | 2/3b | L–M | L | Avoids LLM cost in the pipeline |
| O21 | Volunteer blind-label programme for developer benchmarks (several occupations) | 2 | M | process | Replaces the single-owner label bottleneck |
| O22 | Opt-in aggregate telemetry | 2 | M–H long term | H | D1 |

**Unnecessary manual configuration to remove over time.** Weights in `scoring.yaml`; freshness
days; keyword lists; `max_abs_adjustment`; the personalization bridge switch; the disagreement mode
switch. Users should set *intent* (what, where, contract, distance, how strict). Derived or learned
values should replace the knobs, with developer defaults validated per release.

---

## 7. Implementation options and trade-offs

| Decision area | Option | Benefits | Drawbacks | Cost / complexity | Privacy |
|---|---|---|---|---|---|
| **Personal model** | (a) Keep token averages, turn the bridge on | Trivial | Generalises poorly; company/title tokens are sparse | L | Local |
| | (b) **Rocchio / Bayesian linear on stored embeddings + profile prior** ← recommended | Learns from 1 signal, smooth, cheap, explainable via nearest liked offers | Embedding quality limits it; needs exposure correction | M | Local |
| | (c) Gradient-boosted ranker per user | Powerful with many signals | Needs hundreds of events; overfits; heavier | M–H | Local |
| | (d) LLM judges each offer against preferences | Rich reasoning | Expensive, slow, hard to evaluate, hardware-bound | H | Local if LLM local |
| **Weak filters** | Keep hard | Fewer offers to show | Proven recall loss | — | — |
| | **Ranking evidence + separate lane** ← recommended | Recovers relevant offers without flooding the main list | More offers to store and score; UI work | M | — |
| | User picks strictness ("strict / balanced / open") | User control, simple | One more choice | L (on top) | — |
| **Domain matching** | Curated keyword vocabularies (today) | Explainable | Does not scale across occupations | ongoing H | — |
| | **ROME/ESCO occupations + embedding classifier** ← recommended | Official, domain-agnostic, France-first | Mapping work; non-FT offers need classification | M–H | — |
| | JobBERT-style encoder | State-of-the-art title normalization | New model, size and licence check, benchmark first | M | Local |
| **Evaluation** | Labels only (today) | Rigorous | Not enough labels | — | Local |
| | **Labels for developers + shadow/online on behaviour for users** ← recommended | Works for every user | Behaviour is noisy; needs exposure correction | M | Local |
| **Shared learning** | **Developer releases only** ← recommended now | No new infrastructure, no privacy risk | Slower; developer-data-limited | L | None |
| | Opt-in aggregate telemetry (Prio-like) | Real-world signal across occupations | Server, consent, GDPR, maintenance | H | Aggregates only |
| | Federated training | Strongest learning across users | Overkill, complex, privacy subtleties | Very H | Complex |
| **LLM usage** | In-pipeline gate or enrichment (today, optional) | Catches implicit mismatches | Cost, latency, hardware | M | Local |
| | **On-demand analysis + offline audits + query proposals** ← recommended | Value where a user looks; developer insight | Less automation in the pipeline | L–M | Local |

---

## 8. Prioritized roadmap

The order follows dependencies and the evidence: first stop losing relevant offers and start
recording usable signals, then learn from them, then generalise. It reorders the current P4/P5 and
partly replaces "P4 = tune weights on held-out labels", which finding 1 shows cannot move results
while the filters stay as they are.

### Short term (next 4–8 weeks, measurable on existing data)

| Step | Content | Depends on | Measured by |
|---|---|---|---|
| **S0** | ADR: "learning architecture: personal / engine / market / quality", answering D1–D10 | owner decisions | — |
| **S1** | Signal foundation: impression logging (list positions shown), quick save/hide, application status and opens as derived signals (analysis-time weights) | — | Event volume per active day; no UI regressions |
| **S2** | Structured FT evidence: ROME code and label, experience, education, salary into typed fields and evidence; daily extractor-vs-structured report in Quality | — | Extractor precision/recall vs FT fields (automatic) |
| **S3** | Weak filters → evidence + "Outside your criteria" lane; pre-registered first | S0, D2 | real-v2 tuning: relevant recovered vs non-relevant added (today: 2/7 relevant, 13/60 non-relevant pass); then real-v3 held-out once labelled; synthetic safety 0 regressions |
| **S4** | Feedback with a visible effect by default (bounded, undo), reasons mapped to features; "Votre avis" stays optional, no counters | D3 | Shadow first (S5) |
| **S5** | Shadow champion/challenger + auto-revert health monitor, journaled per strategy version | S1 | Exposure-corrected engagement metrics; guardrails |

### Medium term (2–4 months)

| Step | Content | Depends on |
|---|---|---|
| **M1** | Embedding preference model with profile prior and exploration lane; replaces token personalization once the shadow beats it | S1, S5 |
| **M2** | ROME classifier for non-FT offers; occupation-based domain relevance; keyword vocabularies become a fallback | S2 |
| **M3** | Profile suggestions from behaviour (user-confirmed); re-score rejected snapshots after profile edits | M1 |
| **M4** | Source quality statistics + query value by engagement + adaptive pacing | S1 |
| **M5** | Near-duplicates and reposts; freshness from reposts | — |
| **M6** | Disagreement engine → "Needs your judgement" lane, calibrated from responses (folds into P6 Home) | S5 |
| **M7** | Volunteer blind-label programme across occupations for developer benchmarks | D6 |

### Long term (after 2.0)

- **L1** Opt-in aggregate telemetry (if D1 = yes) feeding developer-side engine improvements.
- **L2** Market insights per occupation and region; career-path suggestions (skill graph v2).
- **L3** Evaluate JobBERT-class encoders against MiniLM on developer benchmarks; adopt only with a
  measured gain.
- **L4** LLM offer analysis (P5) stays on demand; LLM-assisted query and vocabulary proposals go
  through the lab.

**Interaction with the 2.0 plan.** P3 (profile v2, AI setup, wizard) is independent and can continue.
S1, S2 and S5 are small and backend-heavy; under rule R-C2 each needs its UI. S3 changes a contract
(filter outcome semantics, scoring inputs) and needs the plan-and-approve process. The release gate
(P7) should add "personalization health monitor works" and "no new strong rejections".

---

## 9. Short-term improvements vs longer-term capabilities

- **Short term = connect and unblock**: use recorded behaviour, use the FT structured fields, stop
  rejecting on weak evidence, give feedback a visible effect, evaluate in shadow. No new model types,
  no server, no LLM cost.
- **Medium term = learn**: per-user preference model, occupation intelligence, source and query
  learning, profile suggestions.
- **Long term = share and generalise**: opt-in aggregates, a better global engine from several
  volunteers and real-world statistics, richer market intelligence.

---

## 10. Architectural decisions that need your approval

| # | Decision | My recommendation |
|---|---|---|
| **D1** | Is there ever a channel *between* installs (opt-in aggregate telemetry, a server), or is shared intelligence strictly developer releases? | Releases only for 2.0; revisit opt-in, aggregate-only telemetry after 2.0 with a privacy design |
| **D2** | May weak evidence (title words, age, seniority words, inferred education) stop being a rejection, becoming ranking evidence plus an "Outside your criteria" lane? Which constraints stay hard? | Yes; hard = explicit user choices with unambiguous data (contract, max distance with a resolved place, remote country eligibility) |
| **D3** | Should explicit feedback and application status change the user's ranking **by default** (bounded, visible, undoable)? | Yes, after the S5 shadow check |
| **D4** | May *personal* model updates be promoted automatically (bounded, shadow-checked, auto-revert), while *global* strategy changes keep lab + human approval? This amends the spirit of ADR 0011 for Layer 1 only | Yes |
| **D5** | Replace token personalization with an embedding-based preference model (prior = profile) once it wins in shadow? | Yes |
| **D6** | Labels: confirm they are developer and release instruments, not a user prerequisite; replace "the owner's 100 labels" with a volunteer programme across occupations | Yes |
| **D7** | Adopt ROME/ESCO occupations as the primary domain model (France first), with keyword vocabularies as fallback? | Yes |
| **D8** | An exploration lane (a few clearly labelled "Worth a look" offers) in the main results? | Yes, small and labelled |
| **D9** | Which implicit signals may be recorded (impressions, dwell, hide), and how long they are kept; shown in Settings › Data with erase | Impressions + actions; no dwell time at first; same retention and erasure as feedback |
| **D10** | Re-scope P4 ("tune weights on held-out labels") into S1–S5 above; keep P3 unchanged | Yes |

---

## Appendix A. Key evidence

- Owner labels real-v2 v5: 100 labels (11 accept, 87 reject, 2 borderline); held-out 31 (4 accept).
  Gates accept 5 held-out labelled offers (1 relevant); 3 of 4 relevant rejected. H1/H2 ΔAUC 0.0 [0, 0];
  not supported.
- Tuning filter investigation: 7 relevant; 2 pass; rejections: age ×3 (32–57 days, limit 30),
  title ×2. With `freshness_days: 45`: 3 relevant / 18 non-relevant pass; adding "support applicatif":
  3 / 13.
- real-v1 (assisted, exploratory): precision 0.058; rejected-pool reasons: title_keywords 7,041,
  seniority 5,368, published_age 1,766, contract 1,178, location 1,103; gate: experience 1,688,
  education 1,569, skills 830.
- Code: `config/feedback.yaml: personalization_bridge: false`; `config/semantic_config.yaml`
  similarity weight 0.06; `feedback/models.py` behaviour types = viewed, apply_link_opened;
  `collectors/france_travail.py:883–906` keeps only `competences` from the structured payload (the rest
  passes through as `_france_travail_raw` → `jobs.raw_payload`); `discovery/adaptive.py` yield =
  offers collected, France Travail only; `filters.yaml` domain vocabulary centred on IT.

## Appendix B. Not verified

- Exact France Travail field names for experience and education (third-party listings; check
  francetravail.io).
- JobBERT v2 licence and model size.
- Hermes Agent performance claims.
- No live DB statistics were recomputed for this report; volumes quoted come from earlier reports.
