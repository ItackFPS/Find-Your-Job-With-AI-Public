# Architectural decisions for a learning application — corrected version (v5)

Status: **approved by the owner on 2026-11-05 (v5).** The approval covers the decisions' direction
and the owner decisions recorded in §8. It does **not** approve any parameter: every number tagged
[D] stays an initial value until the simulator, the developer corpus or a release measures it (§9).
Not an ADR: approved decisions become ADRs 0014 onwards, one per PR, in the order of §10 (stage L0).
Nothing has been implemented. This is a documented architecture, not a validated one: v5 adds no
experimental result.
Base: public repository `main` @ `6207a7c`. v5 is the last of four review passes over the v1
proposal (`2026-11-05-learning-architecture-proposal.md`); only v5 is kept in the repository, and
the "What changed" tables below record each pass. Sections not repeated here (four-plane table,
reuse inventory) stay as in v1 unless this document says otherwise. The v1 proposal follows the
audit in `2026-11-05-continuous-learning-audit.md`.

### What changed in v5 (owner's fourth review)

| Point | Sections changed | Decisions changed |
|---|---|---|
| A1. S3 is not less biased | §4.5 surfaces table (S3 row), "Strata" (three strata: LB, IM, PF), multi-surface attribution, G3b row | AD-5 |
| A2. S4 pool independent of both rankings | §4.5 surfaces table (S4 row), "Exploration pool (S4)" (new); §4.3 AD-3.3–3.6 exploration bullet | AD-5, AD-3 |
| A3. Thin LB evidence is not validation | §4.5 "Activation states" (new: SHADOW / EXPERIMENTAL / ACTIVE); G3b "too few positives" bullet; "Accepted consequence" | AD-5 |
| A4. Self-reinforcement in the simulator | §4.5 success criteria (scenarios SR-1 to SR-5 added) | AD-5 |
| A5 / B3. Positive attached to one run | §4.5 evidence unit ("Run association", new); AD-5 schema (`shadow_positive`) | AD-5, AD-6 |
| B1. Ordered delivery per offer | §4.6 "Ordering per offer" (new); AD-6 schema; durability tests | AD-6 |
| B2. Client time vs server time | §4.6 "Timestamps: client creation vs server receipt" (new) | AD-6, AD-5, AD-3 |
| Clarif. `viewed` is best-effort | §4.6 "Delivery and durability" (note under the classes table) | AD-6 |
| C1. Delete-all resume state | §4.12 "Delete all application data" (rewritten: external state file, DB-free procedure, data epoch) | AD-12 |
| C2. Erasure marker vs zero rows | §4.12 DB_ERASED step (no marker event in `feedback_events`), verification item 1, "Erasure record" (new) | AD-12 |
| D. Lab purge writers and both `lab.db` | §1.6 (one fact added); §4.12 "Lab purge rules" (rewritten: suspension, lock, no recreation, both locations) | AD-12 |
| Clarif. Reset vs stored personal scores | §4.12 four-actions table (`job_scores.personal_*` row); §4.3 "Reset and stored personal scores" (new) | AD-3, AD-12 |
| Clarif. AD-7 calibration and splits | §4.7 AD-7.2 "Count correction" (pooled offsets), "Data separation" (D-calib sub-splits, on-install split by employer) | AD-7 |
| Clarif. AD-8 profile salary contract | §4.8 "Comparability class"; §8 item 7 | AD-8 |
| Owner decisions | §8 rewritten: items 1–13 recorded as decided; new open items 14–16 | — |
| Knock-on | §5 dependencies; §7 failure modes; §9 parameters; §10 readiness and drafting order | — |

v5 adds no validation result. The one new [V] fact (§1.6, lab paths) was read from code at
`6207a7c`.

### What changed in v4 (owner's third review)

| Point | Sections changed | Decisions changed |
|---|---|---|
| 1. Erasure generation; `lab.db` | §1.6 (two facts added); INV-10; §4.12 four-actions table (lab rows), "Erasure generation" (new), state table rows QUIESCED / DB_ERASED / COMPLETE, verification item 5, "Lab data during erasure" (new; replaces the v3 developer-data bullet in "Other rules") | AD-12 |
| 2. G3b and attribution | §4.5 "Rules" (G3 claim corrected; G3b replaced; multi-surface attribution), remaining-bias item 5, G3b row in the activation table | AD-5 |
| 3. AD-7 evaluation leakage | §4.7 AD-7.2 "Calibration" and "Group fallback" (rewritten), "Data separation" (new); §4.7 success criteria item 1 | AD-7 |
| 4. Hourly salaries | §4.8 conversion table rows `hour` and `part-time` | AD-8 |
| 5. Event durability | §1.6 (one fact added); §4.6 "Delivery and durability" (new); §4.6 success criteria | AD-6 |
| 6. Consistency | §9 AD-7 line (obsolete k = 15, 5 local prototypes and 0.35 removed); §4.7 bias text labels k = 15 as the v2 method | — |
| 7. Salary as UNKNOWN | §4.2 AD-2b "Salary check" (new); §4.8 "Existing filter" (now a prerequisite) | AD-2, AD-8 |
| Knock-on | §5 dependencies; §7 failure modes; §8 open questions; §9 parameters; §10 readiness and drafting order | — |

v4 adds no validation result. New [V] facts were read from code at `6207a7c`.

### What changed in v3 (owner's second review)

| Point | Sections changed | Decisions changed |
|---|---|---|
| 1. Complete erasure | INV-10; §1.6; §4.12 "Erasure procedure" (rewritten as a persistent, resumable procedure); §7; §8; §9 | AD-12 |
| 2. Shadow evaluation bias | §1.6; §4.5 "Outcome sources and remaining bias" (new), G3 row, wording rule; §4.6 event context; §7; §8; §9 | AD-5, AD-6 (context field only) |
| 3. ROME prototype bias | §4.7 AD-7.2 classifier (rewritten: per-code scoring, prototype cap, calibration); §4.7 success criteria (per grand domaine); §7; §9 | AD-7 |
| 4. Salary comparability | §1.4 (salary figures corrected); §1.6; §4.8 "Salary comparability" (new) and the field-coverage note; §7; §8; §9 | AD-8 |
| Correction | §1.4 and §4.8: v2 said salary was "stated" on 564 of 1,161 offers. 564 is the count of non-empty `salaire` objects; an amount is stated on **477 (41.1 %)** | — |
| Readiness | §10 (new) | — |

No other decision changed. v3 adds no validation result: every number tagged [V] below was measured
during this review on the stated data, or read from code. Illustrations from synthetic data are
labelled as such and are not validation.

No repository was modified. The real-data checks in §1 opened a copy of the owner's labelling database
read-only (`mode=ro`).

---

## How to read this document

Every substantive statement carries one tag:

- **[V] Verified fact.** Checked during this review against code at `6207a7c`, real data, or an
  official page. The source is given.
- **[A] Assumption.** Believed but not verified, or verified only partially. Each one says what would
  verify it and what the design does if it turns out false.
- **[D] Proposed design choice.** A decision put forward for approval. Every number tagged [D] is an
  *initial parameter*. Following the owner's instruction, approving a decision's direction does
  **not** approve its parameters. Parameters are fixed later by measurement, the lab or a release.

### Owner approval status (from the review)

| AD | Owner position | What v2 does |
|---|---|---|
| AD-1 Knowledge packs | **Approved** | Unchanged. Pack contents updated by the §1 findings (ESCO becomes conditional). |
| AD-2 Evidence strengths | **Amend** | Offer validity split from user constraints. "Hidden" becomes conservative (§4.2). |
| AD-3 Personal model | **Amend** | Signal table rebuilt on exposure levels. "IPS" claim removed. Ignored signals almost inert. |
| AD-4 Promotion regimes | **Approved** | Unchanged in direction. One sentence now follows AD-5 (see §4.4). |
| AD-5 Shadow and health | **Rework criteria** | New activation rule: minimum exposure, minimum evidence, Bayesian probability of improvement. No time-based fallback. |
| AD-6 Signal capture | **Amend** | Rendered, visible, opened and ignored are defined separately. Only visible exposure is logged. |
| AD-7 Occupation model | **Verify and amend** | Verification done (§1). Similarity, transferable skills and feasibility are separated. Wording rules added. kNN performance design added. |
| AD-8 Market plane | **Amend scope** | Coverage descriptor on every insight. "Observed in the collected sample" is separated from "market demand". |
| AD-9 Self-monitoring | **Approve direction** | Unchanged. Parameters unapproved. New exposure and shadow-health metrics added. |
| AD-10 Labels | **Approved** | Unchanged. |
| AD-11 Lanes | **Approve direction** | Unchanged. Parameters unapproved. Hidden suppression applies to every lane. |
| AD-12 Privacy and retention | **Resolve before implementation** | Resolved in §4.12: backups, WAL, other copies, Reset vs Erase, aggregates. |
| AD-13 Roadmap | **Approve direction** | Stage list updated (§6). |

---

## 1. Verification results (the four external points)

### 1.1 ROME 4.0 and its licence

- **[V]** ROME 4.0 is published on data.gouv.fr by France Travail. The licence is **Licence Ouverte /
  Open Licence (Etalab)**. Downloading also requires accepting a ROME licence document (PDF). Source:
  data.gouv.fr dataset "Répertoire Opérationnel des Métiers et des Emplois (ROME)".
- **[V]** Published counts: 1,923 fiches métiers, 14,330 appellations, 17,193 savoir-faire, 15,699
  savoirs. Last update 20 October 2026. Next update announced for March 2027. Source: same dataset
  page.
- **[V]** France Travail's open-data page lists the arborescences as XLSX. It lists the full data in
  XML, CSV and JSON through an API. It lists correspondence tables ROME↔NAF, ROME↔Formacode V14 and
  FAP/PCS↔ROME. Source: francetravail.org open-data page "Répertoire opérationnel des métiers et des
  emplois".
- **[V]** Four ROME 4.0 APIs exist: Compétences, Métiers, Contextes de travail, Fiches métiers.
  Registration goes through francetravail.io. The limit is 1 call per second. The API dataservice page
  shows the licence as "non renseignée" (not stated). Source: data.gouv.fr dataservice "API ROME 4.0";
  francetravail.io blog "API ROME 4".
- **[A]** The ROME licence PDF imposes no condition incompatible with redistributing derived tables
  inside an installer. Verify: read the PDF before the pack ADR. If false, ship only the open-data
  arborescence and build the rest on the user's machine at first run.
- **[A]** The bulk export holds the same fields as the API. Verify: download the export when building
  the pack. If false, the pack builder (developer side) calls the API at 1 call/s. This is a one-time
  developer cost, never a user one.

**Design consequence. [D]** ROME is the primary occupation scheme. The pack ships the data that is
covered by the Licence Ouverte. Its manifest records the licence, source URL, download date and ROME
version.

### 1.2 Official ROME–ESCO crosswalk

- **[V]** The France Travail open-data page lists **no** ROME↔ESCO table and **no** ROME↔ISCO table.
  Source: francetravail.org open-data page (correspondence tables listed above).
- **[V]** The ESCO "Other crosswalks" page lists crosswalks with O*NET and NACE Rev. 2.1 only. It
  lists none with ROME. Source: esco.ec.europa.eu "Other crosswalks".
- **[A]** A ROME→ESCO alignment was commissioned around 2020–2021. A third-party report describes
  11,096 appellations mapped to 2,942 ESCO occupations. Source: Mindmatcher blog "Alignement ROME vers
  ESCO". I found no official publication or licence for it. Treat it as **unavailable**.

**Design consequence. [D]**
- No decision may depend on an official ROME↔ESCO crosswalk.
- If ESCO is used, any ROME↔ESCO link is developer-built (label-embedding similarity, then a reviewed
  sample). It ships with a measured precision and `kind = "derived"`, never `"official"`.
- Without such a validated link, the two schemes stay independent. Occupation identity uses ROME.

### 1.3 ROME occupation-mobility relationships

- **[V]** ROME fiches describe "mobilités professionnelles envisageables" (career moves considered
  possible). Source: France Travail ROME documentation (open-data page).
- **[A] / unverified.** Whether the open data or the API exposes these moves **as machine-readable
  links to target ROME codes or appellations**. Not verified. Verify: inspect the Fiches métiers export
  or API response when building the pack.

**Design consequence. [D]** v1 used `official = 0.7` for "mobilité" links in relatedness. That term is
**removed** from the base design. Relatedness works without it (§4.7). If the links turn out to be
machine-readable, they are added as one more input with their own weight, validated in the lab like
any other engine change.

### 1.4 France Travail experience and education fields in real offers

Real data: `<owner labelling folder>/data/real-v3/jobs.db`, opened read-only. It holds **1,161** offers
from the France Travail API (`raw_payload`).

| Field | Observed **[V]** | Notes |
|---|---|---|
| `romeCode`, `romeLibelle`, `appellationlibelle` | present on 100 % (1,161/1,161) | structured occupation identity, unused today |
| `experienceExige` | `D` = 435, `E` = 726; no other value observed | D = débutant accepté, E = expérience exigée |
| `experienceLibelle` | "Débutant accepté" 432; "1 An(s)" 152; "2 An(s)" 151; "3 An(s)" 139; "5 An(s)" 105; "Expérience exigée" 46; "0 An(s)" 36; plus "N Mois" variants | the years can be parsed; "Expérience exigée" carries no duration |
| `formations[]` `{niveauLibelle, exigence}` | present on a minority (51 of a 400-offer sample) | `exigence` is S (souhaité), E (exigé) or absent |
| `competences[]` `{code, libelle, exigence}` | 94 of 400 sampled | today's collector keeps only `libelle` (`src/collectors/france_travail.py:883-906`); the full payload is stored |
| `qualificationCode` / `qualificationLibelle` | 167 of 400 sampled | |
| `salaire` object non-empty | 564 of 1,161 | includes objects holding only `commentaire` or benefits |
| `salaire.libelle` (an amount is stated) | **477 of 1,161 (41.1 %)**: Annuel 376, Mensuel 83, Horaire 18 | detail in §4.8 "Salary comparability" |
| `codeNAF` / `secteurActivite` | 270 of 400 sampled | |
| distinct ROME codes | 151 | biased towards IT by the owner's queries |

- **[A]** These shares hold for other occupations and for other users' queries. The sample has a
  single user, skews towards IT, and the 400-offer figures come from a sample. Verify on a developer
  corpus spread across all ROME domains (§4.7 success criteria).

**Design consequence. [D]**
- `experienceExige` and `experienceLibelle` give structured experience evidence for about 63 % of FT
  offers (E). The remaining 37 % (D) say "beginners accepted", which is also useful evidence.
- Education is structured only on a minority. Elsewhere it stays the extractor's job, or UNKNOWN.
- Non-FT sources have no such fields. Their requirements stay with the existing extractor (ADR 0004).

### 1.5 ESCO package size and licence

- **[V]** ESCO v1.2.1 (10 December 2025): 3,039 occupations, about 13,939 skills, 28 languages. The CSV
  download is free and asks for an email address. The download page states **no licence and no
  package size**. Source: esco.ec.europa.eu "Download".
- **[A]** ESCO is reusable under the Commission's reuse policy (Decision 2011/833/EU), which is usually
  applied as CC BY 4.0. The ESCO legal notice itself was not read. Verify: read the ESCO legal notice
  and record the exact attribution text.
- **[A]** Package size is unknown. A French and English subset (occupations, skills, relations, no
  embeddings) probably fits inside the 30 MB bundle budget of AD-1. Verify: download and measure.

**Design consequence. [D]** ESCO is **optional** in the first pack. It is included only after both
[A] items above are verified. Everything in this document works with ROME alone. ESCO adds:
- a multilingual skill identity (for expansion beyond France, O-4);
- essential/optional occupation–skill relations.

ROME's own savoir-faire and savoirs per fiche already cover France.

### 1.6 Code facts used by the decisions below

All **[V]** at public `main` `6207a7c`:
- `numpy` and `onnxruntime` are already dependencies (`pyproject.toml`). Vector work needs no new
  dependency.
- The frontend has no `IntersectionObserver` and no list virtualization. Every card in a list is
  rendered, so "rendered" says nothing about "seen".
- `recordJobEvent` (`frontend/src/api/client.ts:730`) sends `viewed` and `apply_link_opened` from
  `JobDetail.tsx`.
- `feedback_events` is append-only, enforced by triggers. Classes: explicit (`interested`,
  `not_interested`, `ranking_feedback`, `unsure`); behavior (`viewed`, `apply_link_opened`); outcome
  (`application_status_changed`). Source: `src/feedback/models.py`.
- Backups: `<db folder>/backups/<stem>-<UTC>.db`, written with the SQLite backup API. Two are kept
  (`BACKUPS_KEPT = 2`). Migrations back up only when the DB is ≥ 200 MB (`BACKUP_MIN_BYTES`). Cleanup
  backs up first. Source: `src/storage/migrations.py`, `src/storage/cleanup.py`.
- `src/decisions/storage.py` (`purge_text`, `purge_records`) already uses `PRAGMA secure_delete=ON`
  then `PRAGMA wal_checkpoint(TRUNCATE)`. `src/storage/cleanup.py` checkpoints with TRUNCATE and runs
  VACUUM.
- Cleanup protects offers referenced by `job_feedback`, `feedback_events`, `outcome_events`
  (`PROTECTING_TABLES`).
- `job_embeddings.vector` is stored as JSON text.

Added in v3 (all **[V]** at `6207a7c`):
- `recordJobEvent(id, eventType)` sends only `{event_type}` (`frontend/src/api/client.ts:730`). No
  list, surface, position or sort is recorded with `viewed` or `apply_link_opened`.
- `Job` has only `salary_min`, `salary_max`, `salary_currency` (`src/models/job.py:56-58`). There is no
  period, no gross/net basis and no working time.
- The France Travail collector maps **no** salary into `Job`. The full `salaire` object stays in
  `raw_payload` only.
- The Indeed collector maps `min_amount`, `max_amount` and `currency` (`src/collectors/indeed.py:302-304`)
  and keeps no pay interval.
- `matches_salary` compares `job.salary_min` directly with the profile minimum
  (`src/pipeline/filter.py:565`), whatever the period of either value.
- Application-written copies besides `backups/jobs-*.db` (listed by `src/storage/cleanup.py:78-84`):
  - calibration annotation backups `backups/annotations.NNN.csv` under the annotation-assist folder
    (`src/evaluation/annotation_assist.py:347-350`);
  - profile backups written next to the profile (`src/geo/profile_migration.py:178`,
    `src/api/routes/profile.py:113`);
  - the lab database `lab.db` (developer tooling).

Added in v4 (all **[V]** at `6207a7c`):
- The server assigns `event_id` (`uuid.uuid4()`, `src/feedback/service.py:104`) and stamps
  `occurred_at` with the server clock for explicit and behaviour events (`service.py:131`, `:143`).
  The client sends no idempotency key. A retried POST therefore creates a second row. A replayed old
  request receives a **fresh** timestamp, so a timestamp check cannot detect it.
- **(v5)** `src/api/routes/lab.py:30-37`: the API reads `data/lab.db` when it exists, and
  `data/lab/lab.db` only when `data/lab.db` does not. Its docstring states that the isolated lab
  agent's account may write `data/lab/` but nothing beside `jobs.db`. `src/lab/isolation.py:357-359`
  warns when both files exist. `LabStore` defaults to `data/lab.db` (`src/lab/store.py:47`). Both
  files can therefore exist on one install.
- `lab.db` lives at `data/lab.db`, or at `data/lab/lab.db` for the OS-isolated lab agent
  (`src/api/routes/lab.py:30-36`).
  - Its tables: `hypotheses`, `candidate_strategies`, `experiments` (dataset ids, SHA-256 of the
    jobs and labels, `result_json`), `approvals`, `memory_items` (`statement`, `details_json`),
    `memory_status`, `agent_runs` (`report_json`), and `active_strategy`.
  - Triggers make `experiments`, `approvals`, `memory_items` and `active_strategy` immutable or
    append-only (`src/lab/store.py`, `src/lab/memory.py`, `src/lab/promotion.py`).
  - `active_strategy` is the pointer the pipeline honours for the production strategy.

---

## 2. Invariants (revised)

Changes from v1 are marked. All **[D]**.

- **INV-1 (amended). Learning never overrides a user's strong constraint, and never rejects.**
  Personal, market and operational learning may reorder offers and propose changes. They may not
  accept an offer that violates a strong user constraint (AD-2), and they may not reject an offer.
- **INV-1b (new). Offer validity is not a preference.** An offer that is invalid (closed, expired,
  duplicate, malformed: §4.2a) is never shown as an open opportunity. Neither learning, nor a lane,
  nor a user constraint setting can change that. Validity rules are operational. They are not part of
  the strong/weak/unknown scheme.
- INV-2. Nothing personal leaves the install. (Unchanged.)
- INV-3. The stored score stays deterministic. (Unchanged.)
- INV-4. Learned state is derived and can be rebuilt. (Unchanged.)
- INV-5. Every learned effect is visible, explainable and undoable. (Unchanged.)
- **INV-6 (amended).** A new learned behaviour runs in shadow first. **Without sufficient evidence, the
  production ranking stays.** Activation needs positive evidence (AD-5). The absence of negative
  evidence is not enough.
- INV-7. Cold start is today's ranking, bit-identical. (Unchanged.)
- INV-8. The planes stay separate. (Unchanged.)
- **INV-9 (new). No claim stronger than its evidence.** The UI never:
  - presents a local sample as the market (AD-8);
  - presents a skill the profile does not mention as a skill the user lacks (AD-7);
  - calls a heuristic correction "unbiased" (AD-3).
- **INV-10 (new; amended in v3). Erase means erased in every copy the application controls.** That
  covers the database, the WAL and the application's own backups (AD-12). Copies outside the
  application's control are named to the user, never implied away. **The UI never reports an erasure
  as complete until every application-controlled copy has been checked and removed.** An interrupted
  erasure resumes and stays visible until it completes. **Writes created before an erasure can never
  be re-inserted after it.** A server-controlled erasure generation enforces this (AD-12). Client
  timestamps are not used for it.

---

## 3. Planes

Unchanged from v1 §2 (owner did not ask for changes). One clarification **[D]**: the flow-4 metrics
(P→O aggregate counts, such as "share of positives carrying weak flag X") are derived from personal
signals. AD-12 therefore treats them as personal data.

---

## 4. Decisions

### 4.1 AD-1: Knowledge packs — **Approved**

Unchanged from v1, with the content changes from §1:

- **[D]** Pack v1 contents: ROME 4.0 arborescence, fiches, appellations, savoir-faire and savoirs, plus
  precomputed label embeddings. ESCO only under the §1.5 conditions. No crosswalk unless it is
  developer-built and validated (§1.2). No mobility links unless §1.3 is verified.
- **[A]** The ROME-only pack fits the 30 MB bundle budget. Estimate: 14,330 appellation embeddings ×
  384 float32 values ≈ 22 MB uncompressed, plus text tables. Verify by building it. Fallback: ship
  `float16` vectors (≈ 11 MB), or use the R-Q4 download path.
- Parameters (30 MB budget, ≤ 1 s load) remain unapproved initial values.

### 4.2 AD-2: Offer validity, user constraints, and conservative hiding — **Amended**

#### AD-2a. Offer validity rules (operational; new section)

**[D]** These rules decide whether an offer is still a real, open, well-formed opportunity. They are
not about the user. They live in the existing validation, deduplication and expiry code paths, and
they are journaled with their own reason codes. They are never "strong", "weak" or "unknown".

| Rule | Basis | Status today |
|---|---|---|
| Offer closed or expired | `expired_at` set, `valid_through` passed, or source reports closure | **[V]** exists (ADR 0012; expiry work #88) |
| Exact duplicate | existing dedup identity | **[V]** exists (dedup stage) |
| Malformed record | validation stage (missing id, title or URL; unparseable payload) | **[V]** exists (validation stage) |
| Apply link dead | not checked today | **[A]** a possible later O-plane check; no decision here |

- **[D]** Publication age is **not** a validity rule. An old offer may still be open, as the owner's
  labels show. Age is a user-side criterion (`freshness_days`) and is handled in AD-2b.
- **[D]** Validity outcomes are excluded from personal learning, lanes, and the "systematic filter
  error" detector. The O-plane monitors them separately (AD-9: expiry rate, duplicate growth).

#### AD-2b. User constraints: STRONG / WEAK / UNKNOWN

Unchanged in substance from v1 (the classification table of checks stands). Two edits:
- The `published_age` row no longer mentions closed offers. Closed offers are AD-2a.
- INV-1 now names *user* constraints only.

**[V]** Motivation stands. In the owner's labels:
- 5 of 7 relevant tuning offers were rejected by weak checks (age ×3, title ×2);
- 3 of 4 relevant held-out offers were rejected by the gates.

Source: `decision-2026-11-03/REPORT.md`, `FILTER-INVESTIGATION.md`.

**Salary check (new in v4). [D]**
- The salary check compares an offer's normalized salary (`salary_obs`, AD-8) with the profile's
  expectation **only when both fall in the same comparability class**: currency, basis (gross, net or
  unknown), period after the conversions AD-8 allows, and working-time treatment.
- **Incomparable values are UNKNOWN**: neutral, never a violation, and they raise no weak flag. Cases:
  - an hourly offer against an annual expectation;
  - a basis-unknown offer against a gross expectation;
  - a part-time offer without stated hours;
  - a day rate;
  - an expectation with no period or basis (today's profile minimum);
  - a parse failure.
- STRONG and WEAK keep their v1 meaning (strict minimum vs not), but apply only to comparable values.
- **Consequence [V]/[D]:** France Travail amounts carry `basis = unknown` until the §8 open verification settles
  the basis. Today's profile minimum has no period or basis (§1.6). So until both are resolved, the
  salary check is UNKNOWN for nearly every offer. That is the honest outcome; it is not a defect to
  work around.
- **Implementation prerequisite.** Correcting `matches_salary` (§4.8 "Existing filter") must land
  **before**:
  1. any salary-based filtering under AD-2b, in either `reject` or `rank` mode;
  2. the profile salary contract change (§8 item 7) takes effect;
  3. any collector starts mapping new salary values into `Job.salary_min` / `salary_max`.

  **[V]** The France Travail collector maps none today (§1.6). Adding that mapping before the
  correction would start comparing monthly amounts against an annual minimum, and rejecting offers
  on that basis. The salary check is excluded from AD-2b's per-check rollout until the prerequisite
  is met.

**[D]** The lane routing (main list / "outside your criteria" / `below_threshold`), the feature flag
`filters.weak_evidence_mode`, per-check rollout, pre-registration, and lab promotion stay as in v1.
Parameters stay unapproved: lane size 10, score floor 60, proximity floor 0.6, penalty 0.10 each and
0.25 in total, age ramp 1× to 3×.

#### AD-2c. Hidden offers (conservative; replaces v1's "hidden = −1.0 generalising")

**[D]**
- **What Hide does:** removes **that offer** from every list and lane, including its exact duplicates
  under the existing dedup identity. Near-duplicates (AD-9 links) are **not** hidden automatically.
  They show a small "similaire à une offre masquée" note instead.
- **What Hide does not do:** it is list management, not a judgement. By default it produces **no
  generalising negative**: no embedding update, no occupation or company penalty.
- **Optional "Pourquoi ?"** After hiding, a non-blocking chip row offers the existing reasons (too far,
  salary, contract, level, skills, off-domain, other). Choosing a reason records an explicit
  `not_interested` event with that reason. It is then routed like any explicit reason (AD-3), and
  routed **only** to that feature.
- **Pattern rule (very weak, bounded):** when ≥ 3 offers of the same occupation group, or ≥ 3 of the
  same company, are hidden within 30 days, the app **asks** first. Example: "Masquer moins d'offres
  de type X ?", which gives one explicit, confirmable choice. Without an answer, the pattern adds at
  most a structured-feature adjustment of weight −0.2 per hidden offer to that occupation group or
  company. It never touches the embedding, and it stays under the same `cap(n)`.
- **Undo:** "Afficher à nouveau" (`unhidden`) restores the offer and removes any pattern contribution.
- **[V]** A hide lives in `feedback_events`, so the offer is protected from cleanup
  (`PROTECTING_TABLES`). This keeps a hidden offer hidden and stops it reappearing after a re-collect.

### 4.3 AD-3: Personal preference model — **Amended**

The model structure (Bayesian ridge on the embedding with a profile prior, per-value shrinkage on
structured features, an evidence-scaled cap, reason routing, transparency panel) stays in direction.
**All its numbers are unapproved initial values.** The changes:

#### AD-3.1 Signal table, rebuilt on exposure levels

Exposure levels are defined in AD-6. Only **opened** and **ignored** can be behavioural evidence.
Rendered and visible are context.

| Event | Evidence? | Initial weight **[D]** | Routed to |
|---|---|---|---|
| Rendered (card in the DOM) | no | none; not logged | — |
| Visible (≥ 50 % of the card on screen ≥ 1 s, tab visible) | no; exposure context only | 0 | exposure bookkeeping (exploration, ignored derivation) |
| Ignored (see AD-6 for the strict definition) | very weak | **0 at launch** (logged only). Candidate later: −0.05 per qualifying offer, capped at −0.10 per offer, enabled only after the offline replay shows benefit | when enabled: structured features only, never the embedding |
| Opened (detail viewed) | weak positive: curiosity | +0.25, once per offer | embedding + occupation |
| Saved / `to_apply` | positive | +1.0 | all features |
| Apply link opened | strong positive | +1.2 | all features |
| Applied | strong positive | +1.5 | all features |
| Interview / offer / accepted | strongest | +2.0 | all features |
| Employer rejected | none (employer's choice) | 0; earlier positive kept | — |
| Withdrawn | cancels the outcome positive | — | — |
| Interested / "intéressant malgré le score" | explicit positive | +1.2 | all features |
| Not interested, no reason | explicit negative | −1.2 | embedding + occupation |
| Not interested + reason | explicit, targeted | −1.2 | **only** the reason's feature (unchanged routing) |
| Unsure | clears an earlier explicit stance | 0 | — |
| Hidden, no reason | list management | **0** (see AD-2c) | — |
| Hidden + reason | as "not interested + reason" | −1.2 | that reason's feature only |
| Calibration label (tuning split only) | explicit | ±1.2 | all features; held-out never read |

Aggregation per offer (precedence, clamp, recency half-life, profile-change discount) is unchanged.
Its parameters are unapproved.

#### AD-3.2 Position bias: the heuristic is removed, and nothing is called IPS

- **[V]** v1's `1/p(k)`, with `p(k) = 1/log2(k+1)` clipped to [1, 3], is a **position heuristic**.
  It borrows the DCG discount. Its propensities are assumed, not estimated, so it is not inverse
  propensity scoring in the sense of Joachims et al. (2017).
- **[D] At launch: no position reweighting.** The only implicit evidence used at launch is "opened",
  at +0.25, once per offer, under the evidence-scaled cap. The first position and list of every
  exposure are **logged** for later use.
- **[D] Later, as its own release-gated change:** estimate examination propensities from the
  randomized part of the exploration slots (AD-3.4). Two methods are candidates:
  - randomized swaps of adjacent pairs (Joachims et al. 2017);
  - intervention harvesting (Agarwal et al. 2019).

  The result is called an "estimated position correction". The UI and docs claim unbiasedness only
  under that model's stated assumptions, and only after an offline check on logged data.
- **[A]** One user produces enough randomized exposures to estimate propensities for the first ~10
  positions within a few months. Verify by simulating with the owner's exposure volumes once AD-6 has
  logged them. If false, keep no correction. Explicit and outcome signals already carry most of the
  weight.

#### AD-3.3 to AD-3.6

Unchanged in direction:
- updates recomputed from the journals;
- exploration (2 of 20 slots; randomized selection now also feeds AD-3.2). **(v5)** The slots are
  filled from a pool built independently of the production and challenger rankings (AD-5
  "Exploration pool (S4)"). This replaces any rank-based exploration pool;
- diversity guard;
- transparency panel.

One addition **[D]**: while AD-5 keeps a challenger in shadow, the panel shows a neutral status
("L'application observe vos choix ; la personnalisation s'activera quand elle aura fait ses preuves").
It shows no counter and no request for labels (AD-10).

#### Reset and stored personal scores (new in v5) [D]

- After **Reset learning**, the `job_scores.personal_*` values written before the reset stay in the
  table as history (AD-12 four-actions table). **History is not reuse.**
- No ranking, shadow computation, challenger, health statistic or exploration draw reads a
  `personal_*` value written before the latest `learning_reset` marker. Readers select only rows
  whose run started after the marker **and** whose model version matches the current
  `personal_model_state`.
- Rows from before the marker can be shown only on an explicit history view, labelled as such.
- **Test:** reset, run the pipeline, and assert that the final ranking is bit-identical to a run on
  a copy where the pre-reset `personal_*` values were set to NULL.

### 4.4 AD-4: Promotion regimes — **Approved**

Unchanged in direction. One consistency edit **[D]**: v1 activated a personal challenger "after 30
days with no evidence against it". That clause is **removed**, because the reworked AD-5 forbids
time-based activation. Personal algorithm or config changes from releases follow the AD-5 rule like
any challenger.

### 4.5 AD-5: Shadow evaluation and health monitor — **Reworked criteria**

#### Principle

**[D]** The production ranking is the default and stays the default until a challenger has
**sufficient positive evidence**. "No evidence against" never activates anything. This applies:
- to the **first** enabling of personalization (challenger = personalized; production = today's
  ranking);
- to every later personal algorithm or config change shipped in a release.

#### Evidence unit (kept, tightened)

**[D]**
- **Positive:** an offer whose aggregated value reaches y ≥ +1.0 (saved, apply link, applied,
  interested, outcome) within 14 days of the run. An "opened" event alone is **not** a positive.
- **Discordant positive:** the two rankings place the offer ≥ 3 positions apart. Positions beyond 100
  count as 101.
- **Win:** the ranking that placed the discordant positive higher.
- **Clustering control:** at most 3 discordant positives count per session. One binge session must
  not decide activation.
- **Run association (new in v5).** Each positive is attached to exactly **one** run, by a
  deterministic rule, and counts once:
  1. the **anchor run** is the most recent run in which the offer had a **visible** exposure
     (`exposure_log.run_id`) whose `first_visible_at` is at or before the positive's effective time
     (AD-6 "Timestamps");
  2. when no visible exposure was logged before the positive (an S5 notification link, a deep link,
     or an exposure lost because it is best-effort), the anchor run is the most recent run that
     **ranked** the offer and started before the positive's effective time. The positive is then
     attributed to the entry surface (S5 or S0), never to S1;
  3. when no run ranked the offer before the positive, it is not counted (S0);
  4. the positive is judged against the two rankings **stored for the anchor run**
     (`ranking_shadow`), and the 14-day window runs from the anchor exposure to the positive's
     effective time;
  5. **one positive per offer.** Several positive events on the same offer (saved, then apply link,
     then applied) form a single positive. It is anchored on the **first** event that brings the
     aggregated value to y ≥ +1.0. Later exposures of the offer in other runs create no further
     positive. An offer already counted in an earlier shadow window (before a pause or a reset) is
     not counted again in a later window;
  6. the association is recorded once, in `shadow_positive` (schema below), when the positive is
     first counted. It is never recomputed from later data, so a later run cannot move a positive.

#### Outcome sources and remaining bias (new in v3)

**The core problem. [V]** (by construction of the shadow design) The user only ever sees what
production and the fixed surfaces show. Every outcome used to judge the challenger was therefore
produced while production was visible. The shadow compares two rankings on outcomes that one of them
helped to cause.

**[V]** Today no event records where it happened: `recordJobEvent` sends only the event type (§1.6).
Source attribution therefore needs new capture (AD-6 "Event context").

**Surfaces. [D]** Every positive is attributed to the surface where the offer was opened or acted on
**before** the positive. If no open preceded it, the surface of the card it was acted on is used.

| Surface | Lists | How exposure is decided | Expected bias for the shadow test |
|---|---|---|---|
| **S1 main list** | `home_strong` | production order | strongly favours production. A positive can only happen on offers production placed visibly. Mostly measures whether the challenger *keeps* production's good picks |
| **S2 user search** | `jobs_search`, `jobs_filtered` | the user's query and filters; order from the user's sort key | weaker when the sort is by date or distance. **Same as S1** when the sort is by relevance (production score). The sort key is recorded |
| **S3 rule lanes** | `home_outside`, `home_reconversion` | fixed rules (weak flags, similarity band), then production order inside the lane | **intermediate (v5).** Membership is production-independent; the order inside a lane is production's, so the visible positions favour production. Reported as its own stratum (IM), not as less biased |
| **S4 randomized exploration** | `home_explore`, randomized part only (AD-3.4) | uniform (or stratified) random draw from a pool built **without** either ranking (see "Exploration pool (S4)") | the least biased surface. Neither membership nor position depends on either ranking. Small volume (≤ 2 slots per run) |
| **S5 external entry** | notification link, tracker, deep link | notifications are built from production | favours production |
| **S0 unknown** | legacy events with no context | — | excluded from AD-5 counting |

**Rules. [D]**
- AD-5 reports discordant positives, wins and the posterior **per surface** and pooled. The Quality
  page and `personal_health` keep the per-surface counts.
- **G3 is the pooled rule** (all surfaces except S0). Its threshold stays conservative.
- **Correction (v4): production-favouring surfaces do not only make activation harder.** v3 claimed
  they could only make it harder. That is wrong. Two effects act in opposite directions, and their
  net direction is unknown:
  - **against the challenger:** exposure selection. Outcomes exist only for offers production showed,
    so offers the challenger would have promoted from below rarely produce evidence;
  - **for the challenger:** self-reinforcement. The challenger learns from signals on offers
    production showed, and it is then scored on future positives drawn from the same kind of offers
    shown on the same surfaces. It can win by fitting what production's exposure made clickable,
    not by finding better offers. Correlated choices within a period strengthen this.

  Production-favouring surfaces (S1, S5, and S2 sorted by relevance) are therefore treated as
  **potentially inflating** as well as deflating. That is why G3b exists.
- **Strata (three since v5).**
  - The **less-biased stratum (LB)** is S2 with a non-relevance sort (date, distance), plus S4.
  - The **intermediate stratum (IM)** is S3. Lane membership comes from fixed rules, but the order
    inside each lane is production's. IM is reported separately. It is **not** used as evidence by
    G3b, in either direction.
  - The **production-favouring stratum (PF)** is S1, S5, and S2 sorted by relevance.
  - A sensitivity line on the Quality page also shows G3b computed on LB ∪ IM. It is informational
    and never changes a gate.
- **G3b (replaced in v4): probabilistic contradiction guard.** Use independent Beta(1, 1) posteriors
  on the discordant win rates p_LB and p_PF. Block activation when **either** holds:
  - **(a) LB contradicts.** LB holds ≥ n_LB,min discordant positives **and** P(p_LB < 0.5) ≥ c_contra;
  - **(b) the strata disagree.** Both strata hold ≥ n_LB,min discordant positives **and**
    P(p_LB < p_PF − m) ≥ c_het. The challenger then wins clearly more where production shaped the
    exposure than where it did not, which is the signature of self-reinforcement.

  P(p_LB < p_PF − m) is computed exactly or by Monte Carlo from the two Beta posteriors.
  n_LB,min, c_contra, c_het and the margin m are **unapproved parameters**. They are set with the
  AD-5 simulator, in the same way as G2 and G3.
- **When LB has too few positives (rewritten in v5)**, G3b cannot contradict, so it is vacuous.
  Low-volume LB is expected: S4 is at most 2 slots per run. A vacuous G3b is **never** treated as
  a pass. **Normal activation (ACTIVE) requires LB ≥ n_LB,min discordant positives and G3b passing.**
  When every other gate holds but LB is below n_LB,min, the only possible state is EXPERIMENTAL (see
  "Activation states"), which is labelled unconfirmed and never reported as a validation (owner
  decision, §8 item 11).
- **Multi-surface attribution (clarified in v4).** For each positive, the system records every
  surface where the offer was **visible or opened** during the 14 days before the positive
  (`surfaces_seen`). Then:
  - for **G3 (pooled)**, each positive counts **once**, whatever its history;
  - its **primary surface** is the surface of the last open before the positive. When no open
    preceded the positive, it is the surface of the card the action was taken on. The primary
    surface is used for per-surface reporting;
  - for **G3b**, a positive belongs to **LB only when every surface in `surfaces_seen` is in LB**.
    Any visible exposure on a PF surface puts it in PF. Otherwise, any exposure on S3 puts it in IM
    (v5). This is conservative: production's influence on whether the user noticed the offer cannot
    be excluded;
  - a **sensitivity report** shows the per-stratum results under both rules (primary surface vs. any
    PF exposure). A large gap between the two is reported on the Quality page as an attribution
    warning.
- **No leakage.** The challenger ranking used for a run is the one computed **at that run**, from
  signals received **before** it. A positive received after the run is judged against that stored
  ranking (`ranking_shadow`). It is never judged against a model that has already learned from that
  positive.

**Exploration pool (S4) (new in v5). [D]**
- **[V]** No exploration lane exists in the code at `6207a7c`; S4 is new.
- **Eligible pool E** for a run: offers that pass the AD-2a validity rules **and** every STRONG user
  constraint, are not hidden, and were not shown in S4 during the previous 14 days. Nothing else:
  - no production score, score floor or production rank;
  - no challenger score or rank;
  - no weak-evidence penalty (WEAK constraints are production-side scoring inputs).
- **Order of operations.** The S4 draw happens **before** the main list is ranked. The drawn offers
  are removed from the S1 candidate set for that run, so S1 never duplicates them. S1's content
  then depends on the draw, but the draw never depends on S1.
- **Draw.** Uniform over E by default. A stratified draw by occupation group (AD-7, which uses no
  ranking) is allowed. The inclusion probability π of each drawn offer is computed and stored.
- **Position.** The S4 slots have fixed positions in the Home layout. Within S4, order is random.
- **Recorded per run:** the RNG seed, |E|, each drawn offer's π and slot. S4 events carry
  `randomized = true`. Both rankings still compute positions for S4 offers (`ranking_shadow`), so
  discordance is measured as for any other offer.
- **Accepted cost.** Some S4 offers will be poor matches, because no score floor applies. The
  section is labelled "Découverte", and the volume stays at most 2 slots per run.

**Remaining biases, documented and not corrected. [V]** (these follow from the design; their size is
unknown)
1. **Exposure selection.** Outcomes exist only for offers someone showed. Offers the challenger would
   have surfaced but production buried rarely receive outcomes. The test is blind to the challenger's
   main potential benefit, and biased against it.
2. **Position bias inside surfaces.** Higher positions get more attention whatever their quality.
   Within S1 this favours production further.
3. **Positives only.** The unit counts where positives were placed. It says nothing about the
   negatives each ranking puts near the top (precision).
4. **Correlated outcomes.** Positives within a session, and similar offers, are not independent. The
   cap of 3 per session reduces this but does not remove it. The Beta posterior assumes
   independence, so it is **overconfident** to an unknown degree.
5. **Attribution errors.** Primary-surface attribution is a rule (last open before the positive).
   The strict "any PF exposure → PF" rule for G3b reduces this but does not remove it. Exposures that
   happened before visibility logging existed, or on surfaces that are not logged, are invisible to
   both rules.
6. **Time effects.** The user's goals and the offer pool change during the window.

**Wording rule. [D]** The posterior is a **descriptive measure of agreement with observed choices**,
not causal proof. It estimates how often the challenger would have placed the offers the user
actually chose above where production placed them, given those offers were made visible by
production and fixed surfaces. It does not establish that the user would have done better with the
challenger shown.
- Quality page wording: "Sur N offres que vous avez retenues, la version personnalisée les aurait
  classées plus haut dans X cas."
- Do not write: "la version personnalisée est meilleure", or "95 % de chances d'être meilleure".
- Docs and ADRs say "posterior probability of a higher win rate on observed discordant positives,
  under an independence assumption".

**Causal evidence: possible later, not proposed now. [D]** Only showing the challenger produces
causal evidence. For example, team-draft interleaving of production and challenger inside the
exploration slots (Radlinski et al., 2008). Any such design changes what the user sees before
activation, so it needs its own decision (§8).

#### Activation rule: every condition must hold

| Gate | Condition **[D]** (initial values, unapproved) | Why |
|---|---|---|
| G1 minimum exposure | ≥ 14 days in shadow **and** ≥ 10 distinct sessions with visible exposure (AD-6) | no decision on a few visits |
| G2 minimum evidence | ≥ 20 distinct positive offers **and** ≥ 12 discordant positives (after the clustering cap) | no decision on a handful of events |
| G3 statistical evidence | pooled over surfaces S1–S5: posterior P(challenger win rate > 0.5) ≥ 0.95 under a Beta(1, 1) prior on discordant wins | a conservative descriptive threshold, not causal proof (see wording rule) |
| G3b contradiction guard | **ACTIVE needs** LB ≥ n_LB,min discordant positives **and** neither (a) P(p_LB < 0.5) ≥ c_contra nor (b) P(p_LB < p_PF − m) ≥ c_het (with ≥ n_LB,min in PF too). IM (S3) is not used. With LB < n_LB,min, only EXPERIMENTAL is possible. All values unapproved | stops activation driven by production-shaped or self-reinforcing evidence; a vacuous guard is never a pass |
| G4 guardrails | all green over the whole shadow window (v1 list: 0 strong violations shown, diversity guard, lane caps, main-list size within ±30 %) | safety independent of preference |
| G5 health | no pause condition met in the window | consistency with the monitor |

**[V] Arithmetic for G3** (exact, Beta(1, 1) prior, computed for this review):

| Discordant n | Minimum wins to activate | P(better) at that count |
|---|---|---|
| 12 | 9 (75 %) | 0.954 |
| 15 | 11 (73 %) | 0.962 |
| 20 | 14 (70 %) | 0.961 |
| 30 | 20 (67 %) | 0.965 |

For comparison, v1's rule activated on 1 win out of 1 after 30 days. Under Beta(1, 1) that is
P(better) = 0.75.

**[D] Insufficient evidence:** if G1–G3 are not all met, production stays. There is **no deadline and
no fallback.** The shadow keeps running and evidence keeps accumulating.

#### Activation states (new in v5) [D]

| State | Entered when | What the user sees | Cap |
|---|---|---|---|
| **SHADOW** | default; after a pause; after a reset | production ranking | 0 |
| **EXPERIMENTAL** | G1, G2, G3, G4, G5 hold, LB < n_LB,min, **and** the release enables this state | production ranking with a small personal adjustment; label "Personnalisation expérimentale, pas encore confirmée" and an off switch | `cap_exp` ≤ 25 % of `cap(n)`; no offer moves more than 3 positions; the top 3 positions stay production's |
| **ACTIVE** | G1–G5 hold **and** LB ≥ n_LB,min **and** G3b passes | personalized ranking | `cap(n)` |

**Rules for EXPERIMENTAL:**
- It is **never** recorded, displayed or documented as validated. The activation record carries
  `state = experimental_unconfirmed`. The Quality page wording says the adjustment is a trial.
- **Evidence collected while EXPERIMENTAL is split.** On PF and IM surfaces, the challenger now
  shapes part of what the user sees, so positives there would favour it. Those positives are
  logged but **excluded** from G3 and G3b. Only LB positives (S4 and non-relevance S2), plus the
  evidence accumulated during SHADOW, count towards confirmation.
- **Promotion to ACTIVE** happens only when LB reaches n_LB,min and G3b passes on that evidence.
- **Stricter guardrails** than ACTIVE (initial values, unapproved):
  - halve `cap_exp` at ≥ 6 discordant positives with P(worse) ≥ 0.70;
  - return to SHADOW at the second halving, or on any guardrail failure;
  - return to SHADOW automatically after 60 days without promotion.
- **Off switch.** Turning it off returns to SHADOW. It is the user's choice, logged, and does not
  count as a pause.
- **Release flag.** Whether EXPERIMENTAL is enabled at all is a release decision (§8 item 14). With
  the flag off, the rule reduces to "ACTIVE needs a minimum LB volume", and a user without enough
  LB evidence stays in SHADOW.

**[D] Accepted consequence (decided by the owner, §8 item 1):** a low-activity user may never get ranking
personalization. Two things still work for them without AD-5 activation, because each needs no
statistical evidence:
- **profile suggestions** the user confirms (an explicit profile edit);
- **hide** (list management).

#### Health monitor and pause: asymmetric, easier to pause than to activate

**[D]** Rolling 30 days, active personalized vs prior baseline, same statistic:

| Condition | Action |
|---|---|
| ≥ 8 discordant positives **and** P(active win rate < 0.5) ≥ 0.80 | halve the cap |
| the same condition again within 30 days of a halving | **pause** personalization and show a notice with resume and reset |
| top-20 positive rate over 14 days drops > 50 % vs the trailing 60-day median, with ≥ 6 positives in the baseline | halve the cap; a second time: pause |
| any guardrail failure | **immediate pause** |

**[V] Arithmetic:** P(worse) ≥ 0.80 corresponds to ≤ 2 wins of 8 (0.91), ≤ 3 of 10 (0.89), ≤ 4 of 12
(0.87). Pausing needs fewer events (8 vs 12) and a lower probability (0.80 vs 0.95) than activating.

**[D] After a pause:** resuming automatically requires a fresh shadow window that meets the full
activation rule. A manual "Reprendre" is the user's decision. It is allowed, still guarded, and the
monitor stays on.

**[D] Continuous updates within an active version** (the posterior moving as signals arrive) do not
need re-activation. They are bounded by `cap(n)` and watched by the monitor.

#### Success criteria (to run in a simulator before L4 ships) [D]

- **False activation:** with a challenger whose true win rate is 0.5, activation happens in ≤ 5 % of
  simulated 180-day histories. The simulation spans low, typical and high activity levels.
- **True activation:** with a true win rate of 0.70 and typical activity, activation happens in
  ≥ 80 % of histories within 90 days. Typical activity is taken from the owner's logged volumes once
  AD-6 has collected them.
- **Adversarial:** a sequence whose positives contradict the active model triggers a halving, then a
  pause, within the windows.
- **Replay:** the owner's real history causes no pause.
- **Self-reinforcement scenarios (new in v5).** The 5 % false-activation test above assumes
  independent outcomes with a true win rate of 0.5. That is a baseline, **not** sufficient. The
  simulator must also generate the following. Every scenario runs at low, typical and high
  activity, and counts ACTIVE and EXPERIMENTAL activations separately.
  - **SR-1 exposure-fit challenger.** The user's true preference does not depend on production. A
    click model with position bias generates clicks on the offers production shows. The challenger
    is trained on those clicks. Its true win rate under random exposure (S4) is 0.5.
    **Criterion:** ACTIVE ≤ 5 %, EXPERIMENTAL ≤ 10 % of 180-day histories.
  - **SR-2 correlated outcomes.** Positives are correlated within sessions and within groups of
    similar offers, with intra-cluster correlation ρ ∈ {0, 0.2, 0.5}. True win rate 0.5.
    **Criterion:** as SR-1 at every ρ. If ρ = 0.5 fails, the clustering cap or G2 is revised
    through the simulator, never G3.
  - **SR-3 stratum disagreement.** The challenger wins 0.75 of PF positives and 0.5 of LB positives.
    **Criterion:** G3b blocks ACTIVE in ≥ 80 % of the histories in which LB reaches n_LB,min.
  - **SR-4 thin LB.** True win rate 0.5, LB stays below n_LB,min. **Criterion:** no ACTIVE; at least
    90 % of EXPERIMENTAL entries return to SHADOW (halving, pause or 60-day timeout).
  - **SR-5 feedback loop during EXPERIMENTAL.** The challenger's adjustment changes what is shown
    on PF surfaces. **Criterion:** with the exclusion rule on, promotion rates match SR-4. A
    diagnostic run with the rule off shows how much PF evidence would have inflated promotion.
  - The click-model parameters (position bias, session length, positives per session) come from
    the owner's AD-6 logs once they exist **[A]**. Until then the scenarios run over a grid, and
    the gate parameters are set to pass the worst grid point.

**[A]** Typical activity produces ≥ 12 discordant positives within 90 days. Unknown until AD-6 logs
real volumes. If false, G2 is re-examined **through a release and the simulator**, never by relaxing
G3.

#### Schema

As v1 (`ranking_shadow`, `personal_health`). `personal_health` gains `posterior_p REAL`,
`sessions INT`, `positives INT`, and **(v5)** `activation_state TEXT` (shadow, experimental_unconfirmed,
active, paused) plus per-stratum counts (LB, IM, PF).

**(v5)** `shadow_positive(job_id PRIMARY KEY, anchor_run_id, first_positive_event_id, effective_at,
anchor_rule, primary_surface, stratum, surfaces_seen_json, discordant, winner, activation_state_at,
counted_in_window)`. One row per offer; written once when the positive is first counted. It is
derived personal data: erased by "Erase my activity" and cleared by Reset (AD-12).

`ranking_shadow` gains **(v5)** `s4_seed`, `s4_pool_size` per run, and `s4_pi` per S4 offer.

### 4.6 AD-6: Signal capture — **Amended**

#### Exposure levels [D]

| Level | Definition | Logged? | Evidence? |
|---|---|---|---|
| **Rendered** | the card exists in the DOM | no | no. **[V]** no virtualization today, so every card of a list is rendered |
| **Visible** | ≥ 50 % of the card is inside the viewport for ≥ 1 s continuously, while `document.visibilityState === "visible"` | yes: `exposure_log` | no (context only) |
| **Opened** | detail page or panel viewed (`viewed`), or apply link opened | yes: `feedback_events` (existing) | yes, weak positive |
| **Ignored** | visible in an **engaged session** and not opened, saved, hidden or rated in that session | derived, not stored as an event | very weak; inert at launch (AD-3.1) |

**Engaged session [D]:** a session in which the user opened, saved, hid or rated at least one *other*
offer in the *same list*. That interaction shows the user was examining the list. It turns "visible"
into a genuine opportunity to act.

**Ignored rules [D]:**
- An offer counts as ignored only after ≥ 3 distinct engaged sessions. When enabled, its
  contribution is capped per offer (AD-3.1).
- Ignored is never derived for exploration slots that the user may not have read as normal results.
  (Randomized slots feed AD-3.2 instead.)
- Ignored never affects the embedding. Repeated ignoring stays weak by construction, because the cap
  is per offer and not per session.

#### Implementation outline [D]

- One shared `IntersectionObserver` per list with `threshold: 0.5`. A per-card timer starts on
  intersection and stops on exit or on a `visibilitychange` to hidden.
- **[V]** Neither the observer nor virtualization exists today. Both are new frontend code. No
  dependency is needed (browser API).
- Logging is batched and sent by `navigator.sendBeacon`, at most once every 5 s. It is
  fire-and-forget like `recordJobEvent`.
- `exposure_log(session_id, list, job_id, first_visible_at, best_position, visible_ms_total,
  max_ratio, run_id, PRIMARY KEY(session_id, list, job_id))`, index `(job_id)`.
- Retention is set by AD-12.
- Session id: random per browser tab session, not derived from anything personal.
- **Event context (new in v3). [D]** Every `viewed`, `apply_link_opened`, saved, hidden or explicit
  event carries `surface` (S1–S5 list id), `position` (when shown in a list), `sort_key` (for S2),
  `randomized` (true for S4 randomized slots), and `session_id`. They are stored in the existing
  `feedback_events.metadata_json` (**[V]** that column exists), so no column change is needed. The
  frontend passes the context from the list into the detail page. Events without context are S0.

#### Delivery and durability (new in v4) [D]

**Problem. [V]**
- `sendBeacon` queues a request without reporting whether the server stored it.
- Today the client sends no idempotency key (§1.6), so a retry duplicates the event.

**Two delivery classes:**

| Class | Events | Delivery | Loss acceptable? |
|---|---|---|---|
| **Durable** | explicit (`interested`, `not_interested`, `unsure`, `ranking_feedback`, reasons), `saved` / `unsaved`, `hidden` / `unhidden`, application status changes, calibration answers, `apply_link_opened` (strong positive) | acknowledged POST from a persistent outbox, retried, idempotent | **no** |
| **Best-effort** | `exposure_log` batches, `viewed` | `sendBeacon` or POST, fire-and-forget | yes |

**A lost `viewed` is neutral (v5). [D]** `viewed` is best-effort. Its absence never produces a
negative or an "ignored" signal, and never counts against an offer:
- "ignored" (AD-6 exposure levels) is derived only from logged visible exposures **and** logged
  engaged-session interactions, and it is inert at launch;
- a durable action on an offer with no logged `viewed` is accepted as is. The offer is treated as
  opened for AD-3, without inventing a `viewed` row;
- AD-5 run association falls back to the ranking rule when exposure was lost (AD-5 evidence unit).

**Durable delivery protocol:**
1. On the user's action, the frontend creates a `client_event_id` (UUIDv4) and records the
   `erasure_generation` it currently holds (AD-12). It writes the event to an **outbox in IndexedDB**
   **before** sending.
2. It POSTs the event. The server inserts it with `client_event_id` under a **unique index**. A
   repeated POST with the same id returns the stored event (HTTP 200) and inserts nothing.
3. The server replies only **after commit**. The reply carries the stored `event_id` and the
   generation.
4. The frontend removes the outbox entry **only on that acknowledgement**. It retries with
   exponential backoff on network errors, timeouts and 5xx. It also retries at every app start and
   when the page regains focus.
5. Other replies:
   - 4xx validation errors are shown to the user and removed from the outbox, never silently;
   - **409 `erasure_generation_mismatch`** removes the entry silently, because the activity was
     erased (AD-12);
   - 423 (erasure in progress) keeps the entry, and retries after the erasure completes. The entry
     is then rejected with 409.
6. **UI state:** an action shows as pending (subtle indicator) until acknowledged. "Enregistré" is
   shown only after the acknowledgement. A visible notice appears if entries stay unacknowledged for
   more than a set time (unapproved).
7. **No IndexedDB** (private mode, storage blocked): the outbox falls back to memory, with retries
   while the page is open. The UI says actions may be lost if the window closes before they are
   confirmed. **(v5, owner decision §8 item 12)** In this mode the UI never claims durability: a
   permanent warning stays visible, and "Enregistré" still means acknowledged by the server only.

**Schema.** `feedback_events` gains `client_event_id TEXT` and `generation INTEGER`, added with
`ensure_columns`. **(v5)** It also gains `client_instance_id TEXT`, `client_seq INTEGER`,
`client_created_at TEXT`, `client_time_trusted INTEGER` and `superseded INTEGER NOT NULL DEFAULT 0`,
with an index on `(job_id, client_instance_id, client_seq)`. There is a partial unique index on `client_event_id WHERE client_event_id IS NOT
NULL`. Legacy rows keep NULL. Inserts are unaffected by the append-only triggers (they block UPDATE
and DELETE only).

**Server-side writers** (outcome recording, migrations) generate their own `client_event_id`
deterministically from their natural key. An example is the existing `outcome_event_id` link. Replays
are then idempotent too.

#### Ordering per offer (new in v5) [D]

**Problem.** The outbox retries, and several tabs can send. Without an order, "hide" then "unhide"
can arrive as "unhide" then "hide", and the offer ends hidden. A replay of an old event must not
move state either.

**State families.** Events that set a state come in families. Within a family, only the latest
event decides the state:
- save: `saved` / `unsaved`;
- hide: `hidden` / `unhidden`;
- explicit stance: `interested` / `not_interested` / `unsure`;
- application status (tracker statuses).

**Client side.**
- Each browser profile holds a `client_instance_id` (random UUID, created with the IndexedDB
  store) and a **monotonic counter** `client_seq`. The counter is incremented inside an IndexedDB
  transaction, so tabs of the same browser share it.
- Every durable event carries `client_instance_id`, `client_seq`, `client_created_at` and the
  generation.
- The outbox delivers **one event at a time per offer** (FIFO per `job_id`). A later event for the
  same offer waits until the earlier one is acknowledged or definitively rejected. Tabs coordinate
  through the Web Locks API (`navigator.locks`, a browser API, no dependency), so only one tab sends
  for a given offer at a time.

**Server side (defence in depth, for out-of-order arrival anyway).**
- On insert, the server checks the same family, offer and `client_instance_id` for a stored event
  with a **higher** `client_seq`. If one exists, the new row is stored with `superseded = 1`. The
  flag is set at insert and never updated, so the append-only triggers are untouched.
- **Current state** of a family = the latest row with `superseded = 0`, in server insert order
  (`rowid`).
- **Replays** with an existing `client_event_id` insert nothing and return the stored row (v4). They
  cannot move state.
- **Across browser profiles** (two browsers on the same install), there is no shared counter. Server
  insert order decides. **[A]** Concurrent contradictory actions from two browsers within the
  retry delay are rare for a single local user. The limit is documented, not solved.
- **Learning** (AD-3 aggregation) reads a family's events in the same effective order. Superseded
  rows stay as history but never decide a state.

#### Timestamps: client creation vs server receipt (new in v5) [D]

**[V]** Today the server stamps `occurred_at` on receipt (§1.6). With an outbox, receipt can come
hours or days after the action.

**Two timestamps per event:**

| Field | Set by | Meaning | Used for |
|---|---|---|---|
| `occurred_at` (existing column; meaning unchanged) | server, at insert | server receipt time | retention clocks, delivery health (outbox lag), erasure bookkeeping, server-side ordering across browser profiles |
| `client_created_at` (new column) | client clock, when the user acted | creation time as reported by the client | the event's **effective time**, when trusted |

**Trust rules:**
- `client_created_at` is **never** used for the erasure generation, idempotency, retention or
  erasure decisions. The generation alone decides whether a write is accepted (AD-12).
- It is **trusted** when it lies within [`occurred_at` − max_lag, `occurred_at` + skew]. It must
  also be at or after the start of the run that produced the offer's exposure, when one is known.
  Initial values, unapproved: max_lag = 30 days (the outbox's maximum age), skew = 5 minutes.
- When untrusted, the raw value is kept, `client_time_trusted = 0` is stored, and the effective time
  falls back to `occurred_at`.
- **Effective time** = `client_created_at` when trusted, else `occurred_at`.

**Where each is used:**
- **Recency** (AD-3 half-life): effective time.
- **AD-5 windows** (14-day positive window, `surfaces_seen` window, run association): effective
  time.
- **Action order** within a family: `client_seq`, not timestamps (see "Ordering per offer").
- **User-behaviour statistics** (per-day counts, ops Flow-4 metrics): effective time.
- **Delivery statistics** (outbox lag = `occurred_at` − `client_created_at`): both, trusted rows
  only.
- **Exposure batches:** `first_visible_at` is client time and follows the same trust rule against
  the batch's receipt time.

Save, Hide, active questions and "no counters / quotas" stay as in v1. Hide semantics follow AD-2c.
Active-question parameters (1 per visit, 3 per week) are unapproved.

#### Success criteria [D]

- UI test with a mock observer and mock beacon:
  - a card scrolled past in < 1 s is **not** logged;
  - a card visible ≥ 1 s **is** logged;
  - nothing is logged while the tab is hidden.
- No blocking work on render or scroll. Observer callbacks stay ≤ 1 ms each (measured in a browser
  profile).
- Lost exposures on a crash are acceptable. Lost explicit events are not.
- **Durability tests (v4):**
  - kill the server between receiving and committing a durable event, and assert that the client
    retries and exactly one row exists;
  - send the same `client_event_id` twice, and assert one row and the same `event_id` returned;
  - close the page with an unacknowledged entry, reopen it, and assert that the entry is delivered;
  - replay an entry from a previous erasure generation, and assert a 409 and no row.
- **Ordering and time tests (v5):**
  - deliver "unhide" (seq 2) before "hide" (seq 1) for the same offer, and assert the offer ends
    visible, with the "hide" row stored as superseded;
  - replay an old "hide" after "unhide", and assert no new row and the offer still visible;
  - two tabs act on the same offer, and assert one sender at a time and a consistent final state;
  - a `client_created_at` 40 days before receipt, or 1 hour after it, is stored as untrusted and the
    effective time equals `occurred_at`;
  - a lost `viewed` followed by `saved` produces one positive and no negative.

### 4.7 AD-7: Occupation model — **Verified and amended**

#### AD-7.1 Schemes and sources

- **[V]** ROME 4.0, Licence Ouverte (§1.1). **[D]** It is the primary scheme.
- **[A]** ESCO licence and size (§1.5). **[D]** Optional; included only when verified.
- **[V]** No official ROME↔ESCO crosswalk is published by France Travail or ESCO (§1.2). **[D]** Any
  link between the schemes is `kind = "derived"`, ships with a measured precision, or is omitted.
- **[A]** Machine-readable mobility links (§1.3). **[D]** Not relied on.
- **[A]** ROME 4.0 keeps the arborescence where a code's letter is the grand domaine and its first
  three characters the domaine professionnel, as with `M1810`. **Verify** at pack build. **[D]** The
  occupation groups used by the diversity guard, the market tables and the relatedness fallback are
  taken from that arborescence, whatever its exact counts turn out to be.
- **[D]** Tables stay scheme-agnostic (v1 design), so later schemes are added as rows.

#### AD-7.2 Assignment and its performance design

1. **Structured. [V]** `romeCode` is on 100 % of FT API offers in real-v3. **[D]** Confidence 1.0,
   method `structured`.
2. **Classifier, prototype-based, with per-code scoring (rewritten in v3) [D].** This replaces
   v1's kNN over all stored offers, and v2's top-k vote over prototypes.

   **Is the top-k vote biased towards codes with many prototypes? Yes.**
   - **[V] (by construction)** A top-k vote counts neighbours. A code with *n* prototypes can place
     up to *n* of them among the k nearest; a code with one prototype can place at most one. With
     k = 15 (v2's value), a single-prototype code can win only when few other codes reach the top 15. The bias is
     a property of the method, independent of the data.
   - **[V] (arithmetic)** ROME 4.0 averages 14,330 / 1,923 ≈ 7.5 appellations per fiche (§1.1 counts).
   - **[A]** The per-code distribution of appellations is skewed. The ROME data was not downloaded in
     this review. **Indirect evidence [V]:** in real-v3, the distinct appellations *observed* per code
     range from 1 to 17 across 151 codes; 94 codes show one, and one code shows 17. That reflects the
     offers collected, not ROME's lists. Verify on the ROME export at pack build.
   - **Synthetic illustration (not validation).** Script: a local simulation (not committed). Random
     geometry, 300 codes, Zipf-distributed prototype counts (45 % of codes with one prototype),
     k = 15 (the v2 method, now withdrawn). On uninformative random queries, the top 10 % of codes by prototype count won 72 % of
     the time under the top-k vote, 44 % under max-similarity, and 24 % under a vote divided by √n.
     The same run gave 0 % accuracy for single-prototype codes under the top-k vote. It shows the
     direction of the effect only. It says nothing about ROME's real geometry or accuracy.
   - **[V] (by construction)** Max-similarity per code also favours codes with more prototypes,
     more weakly: the maximum of more samples is larger (an extreme-value effect).

   **Design. [D]**
   - **Equal prototype budget per code.** At pack build, each code's appellation embeddings are
     reduced to at most **M = 3** prototypes (k-medoids). Codes with fewer keep theirs. Local
     prototypes (centroid + medoids of the user's structured FT offers) are capped at **L = 3** per
     code, and added only for codes with ≥ 5 local offers. This shrinks the count ratio between codes
     from up to tens-to-one to at most 6-to-1.
   - **Per-code score, not neighbour votes.** `s_code = mean of the top-min(2, n_code) cosine
     similarities` between the offer and that code's prototypes. Averaging the best two damps the
     extreme-value effect. Codes compete on one score each.
   - **Count correction, if still needed.** At pack build, measure on held-out FT offers whether
     `s_code` still rises with prototype count. Measure it on the residual after controlling for the
     true code. If it does, subtract an offset fitted on held-out data. The offset is shipped in
     the pack and recorded in the manifest.
     **(v5) Offsets are pooled, not 1,923 free values.** Per-code offsets fitted from few examples
     are unstable. The offset is modelled as a function of the prototype count (one value per
     count bucket 1, 2, 3), with a hierarchical shrinkage code → domaine professionnel → grand
     domaine. A code's own deviation is kept only when it has at least n_offset,min D-calib
     examples (unapproved). Otherwise it inherits its group's value.
   - **Data separation (new in v4).** Four uses of data are kept disjoint:

     | Split | Used for | Never used for |
     |---|---|---|
     | **D-design** | choosing the method's design: M, L, top-m, the number of codes in the softmax, which features | fitting calibration; reporting results |
     | **D-calib** | fitting the count offset, the softmax temperature, the isotonic tables (code level and group level), and the thresholds τ_code, δ, τ_dp and τ_gd. **(v5)** Split internally, see below | building prototypes; reporting results |
     | **D-test** | the final, independent evaluation, reported per grand domaine | any fitting or choice. If the method or any parameter changes after D-test results are seen, a **fresh** D-test is required (a later collection period). D-test is then "used" |
     | **Prototype sources** | pack prototypes come from ROME appellation labels, not from offers. Local prototypes come from the user's own FT offers at runtime | — |

     - **How the splits are drawn:**
       - developer France Travail corpus, stratified by grand domaine;
       - split by employer, so one employer is never on two sides;
       - near-duplicates removed **across** splits (embedding cosine ≥ 0.95 with the same title);
       - D-test from a **later collection period** than D-design and D-calib, to also measure drift.
     - **D-calib internal split (new in v5).** Fitting several things on the same D-calib invites
       over-fitting, especially the thresholds. D-calib is split by employer into:
       - **D-calib-A:** count offsets and the softmax temperature;
       - **D-calib-B:** the isotonic tables and the thresholds, fitted on scores already corrected
         with D-calib-A's offsets and temperature.
       When a grand domaine has too few examples for a two-way split, k-fold **cross-fitting** by
       employer (k = 5, unapproved) is used instead. Each fold's isotonic map is fitted on the
       other folds, and the thresholds are chosen on the out-of-fold predictions.
     - **Simulated local prototypes:** developer tests build them only from D-design offers. Their
       effect is measured on D-test.
     - **On a user's install** (the O-plane accuracy monitor, AD-9), 20 % of the local structured FT
       offers are **never** used as local prototypes. **(v5)** The held-back part is drawn **by
       employer**: all of an employer's offers fall on the same side. Near-duplicates (same title
       and embedding cosine ≥ 0.95) are removed from the held-back part when a near-duplicate sits
       on the prototype side. The draw is made once and stored. Accuracy is measured only on that
       held-back part. An offer is never scored against prototypes built from itself.
   - **Code-level probabilities.** A softmax over the top 10 codes' scores, with temperature T fitted
     on D-calib, gives raw code probabilities `q(c)`. An isotonic map fitted on D-calib turns the
     top-1 `q` into a calibrated `p_code`. The map is fitted per grand domaine when that domaine has
     ≥ 200 D-calib offers, and globally otherwise. **No threshold is used uncalibrated.**
   - **Group-level probabilities (clarified in v4).**
     - The raw probability of a group is the **sum** of the raw code probabilities of its codes
       within the softmax set: `q(g) = Σ_{c ∈ g} q(c)`. A group is a domaine professionnel (dp) or a
       grand domaine (gd). Codes outside the top 10 contribute 0, which slightly under-estimates
       `q(g)`.
     - Summed probabilities are **not** calibrated automatically. A separate isotonic map per level
       (dp, gd) is fitted on D-calib. It relates the top group's `q(g)` to the event "the true code
       belongs to that group". This gives `p_dp` and `p_gd`.
   - **Decision cascade** (thresholds fitted on D-calib, all unapproved):
     1. assign the **code** when `p_code ≥ τ_code` and the margin rule passes. The margin rule
        abstains when top-1 and top-2 are within δ **and** lie in different dp;
     2. else assign the **domaine professionnel** (`code = NULL`, method `group_dp`) when
        `p_dp ≥ τ_dp`;
     3. else assign the **grand domaine** (method `group_gd`) when `p_gd ≥ τ_gd`;
     4. else **unknown**, with the keyword fallback.

     The assigned level and its calibrated probability are stored in `job_occupations`. A group is
     enough for the diversity guard, the market tables and the similarity fallback.
   - **Domain gate** (AD-7.5) uses the **calibrated** probability (initial threshold 0.8, unapproved).

   **Cost estimate [A], to measure:**
   - ≤ (3 + 3) × 1,923 ≈ 11.5k prototypes × 384 float32 ≈ 18 MB, down from v2's ≈ 37 MB;
   - ≈ 4.4 M multiply-adds per offer;
   - new offers only, batched.

   No latency target is set before measuring. The budget stays assignment ≤ 5 % of run time.
   Embeddings are reused from the semantic stage (**[V]** stored as JSON text, so parsing happens only
   on backfill).
3. **Unknown:** keyword vocabularies as today.

#### AD-7.3 Profile occupations

Unchanged from v1 (the picker, `targets.occupations`, P3 dependency).

#### AD-7.4 Three separate concepts (replaces v1's single "relatedness and reachability")

| Concept | Question | Inputs **[D]** | Output | Used in |
|---|---|---|---|---|
| **Occupation similarity** | How close is occupation A to occupation B? | same code (1.0); same domaine professionnel; same grand domaine; overlap of ROME savoir-faire and savoirs between the two fiches; local market co-occurrence (AD-8, minimum support, coverage-flagged); cosine of local centroids. Combination and weights are lab-tuned. Mobility links only if verified (§1.3) | `similarity(a, b) ∈ [0, 1]` | `domain_relevance`; domain-gate condition; "Reconversion possible" candidate set |
| **Transferable skills** | Which skills the user has *evidenced* are useful for this occupation or offer? | profile `experiences[].skills`, `responsibilities`, `skills`; matched to the fiche's savoir-faire and savoirs and the offer's `competences` (exact, then synonym, then embedding above a measured threshold) | a list of matched skills, each with the profile entry that evidences it | explanation; "Reconversion possible" ordering |
| **Transition feasibility** | Which stated requirements does the user meet, which are unconfirmed, which conflict? | offer requirements: FT `experienceExige`/`experienceLibelle`, `formations[].niveauLibelle` + `exigence`, `competences[].exigence` (**[V]** these fields exist; coverage in §1.4); otherwise the ADR 0004 extractor | per requirement: **satisfied**, **à vérifier** (profile silent), or **écart** (profile explicitly contradicts: e.g. 1 year of experience stated vs 5 years *exigée*) | explanation; existing ADR 0004 gate (unchanged); never a new rejection |

**Rules [D]:**
- **A skill the profile does not mention is "not stated", never "missing".** The profile has no field
  for "I lack X". So, in practice, a skill is never shown as a gap. Only experience and education can
  produce an *écart*, and only from explicit profile values.
- **Wording.**
  - Do not use: "Accessible : 7/10 compétences clés".
  - Use: "7 compétences de votre profil correspondent à ce métier. 3 autres demandées ne figurent pas
    dans votre profil — à vérifier."
  - Plus a link "Compléter mon profil".
- **Profile completeness** is shown next to any feasibility statement. Example: "Votre profil liste
  4 compétences ; les comparaisons sont plus fiables avec un profil complet."
- **Feasibility is never a gate** and never scored in a new way. Mandatory requirements keep their
  current ADR 0004 behaviour.
- **"Reconversion possible" lane [D]:**
  - entry: similarity in a middle band, ≥ 2 transferable skills, no *écart* on a mandatory (E)
    requirement;
  - ordering: by transferable skills, then score;
  - thresholds unapproved.

#### AD-7.5 Use in scoring

Unchanged in direction (`domain_relevance` from similarity; domain gate only at confidence ≥ 0.8,
similarity below a low threshold, and not targeted; through the lab). Thresholds unapproved.

#### Success criteria [D]

- Structured coverage ≥ 99 % of FT API offers. **[V]** 100 % on real-v3 (one user's sample).
- **Classifier evaluation on two corpora (amended in v3):**
  1. **D-test only** (see "Data separation"): France Travail offers with the code hidden, from a
     **developer corpus stratified by ROME grand domaine**, not the owner's IT-biased sample. They
     are split by employer, de-duplicated across splits, and drawn from a later period. Group-level
     accuracy and calibration (dp, gd) are reported alongside the code level;
  2. a small labelled sample of **non-FT** offers, because writing styles differ.

  **Report, per grand domaine and overall:**
  - n;
  - top-1 and top-3 accuracy at code level;
  - accuracy at domaine-professionnel level;
  - abstention rate, and accuracy on the non-abstained offers;
  - group-fallback rate;
  - calibration: reliability table and expected calibration error;
  - **accuracy by prototype-count bucket** (1, 2, 3, ≥ 4 before reduction). This is a direct test
    of the count bias;
  - **confusion between related occupations:** the share of errors that fall in the same domaine
    professionnel, the same grand domaine, or elsewhere, plus the 20 most frequent confused code
    pairs. An error inside the same domaine is less harmful than a cross-domain error. The two are
    reported separately and never merged into one accuracy figure;
  - the same table for the top-k vote baseline, so the per-code design is compared, not assumed
    better.

  **No target is set before this baseline exists.** Proposed acceptance shape, values to be fixed
  after the baseline:
  - no grand domaine with code-level top-3 accuracy below a floor;
  - no domaine with calibration error above a ceiling;
  - no significant downward trend of accuracy with prototype count.

  A domaine that fails gets the group fallback only (no code-level assignment) until fixed.
- **Transferable skills wording:** UI test. No string containing "manque", "manquant" or "missing"
  is rendered for skills.
- **Multi-occupation self-check** (6 fixture profiles, 600 fixture offers): as in v1.
- **Performance:** assignment ≤ 5 % of run time on the reference laptop; memory measured and reported.

### 4.8 AD-8: Market plane — **Amended scope**

#### Principle [D]

The market tables describe **the offers this installation collected**. That is a sample shaped by the
user's sources, queries, geography and history. It is never a census. Every insight carries its
coverage, and the UI separates three levels of claim.

#### Coverage descriptor (attached to every insight) [D]

| Field | Content |
|---|---|
| `n_offers` | offers behind the figure (after dedup) |
| `sources` | sources and their shares (e.g. France Travail 82 %, HelloWork 18 %) |
| `geography` | départements or regions represented, with their counts; remote share |
| `period` | first and last collection dates; number of runs |
| `collection_scope` | the queries, occupations and radius that produced the offers; flag `in_scope` when the insight's occupation or area is inside what was searched |
| `scope_changes` | dates where the collection configuration changed inside the period (queries, sources, radius) |
| `field_coverage` | share of offers carrying the field (e.g. an amount stated on 41 %), and for salary the share in the comparability class used (see "Salary comparability") |

**[V]** Field coverage matters. In real-v3, an amount is stated on 477 of 1,161 FT offers (41.1 %).
(v2 said 564, which counted non-empty `salaire` objects, including those with no amount.) A salary
figure therefore describes well under half the offers, before the comparability rules below remove
more.

#### Three levels of claim [D]

| Level | UI wording pattern | Minimum conditions (unapproved) | Allowed uses |
|---|---|---|---|
| **Observed** | "Dans les N offres collectées par l'application (sources, zone, période)…" | n ≥ 5 (as v1); `in_scope` | Skill Graph, Insights, descriptive figures |
| **Observed trend** | "Parmi les offres collectées, X augmente depuis…" | as Observed, plus **no scope change** in the compared periods, plus n ≥ 20 per period | Insights |
| **Market indication** | "Indication sur le marché (échantillon partiel)…" | n ≥ 30 for the occupation × area × period; `in_scope`; field coverage ≥ 50 % for the field used; ≥ 1 run per week over the period | salary realism hint; market term in occupation suggestions |

- **Salary advice [D]:** only at the Market-indication level. Always phrased as "parmi les offres
  affichant un salaire". Never "le salaire du marché". Below that level the Profile page shows the
  distribution with its descriptor and gives no advice.
- **Occupation suggestions and transitions [D]:** ordered by similarity and transferable skills
  (AD-7). Demand appears only as context. Below the Market-indication level the text reads "données
  insuffisantes pour juger la demande". Demand never ranks an out-of-scope occupation above an
  in-scope one.
- **Out-of-scope [D]:** an occupation or area outside `collection_scope` gets no demand statement.
  Its offers came in by accident of the queries, not by sampling.
- **Engine use of market data [D]** (co-occurrence and centroids inside similarity, flow M→E): minimum
  support (unapproved: 20 offers), weight lab-tuned, and only for pairs `in_scope`. The bias towards
  the user's searches is documented in the pack ADR.
- **[A] External calibration, possible later.** The France Travail API may return a total match count
  for a query (for example in a `Content-Range` header). The data.gouv dataset "Offres d'emploi
  diffusées à France Travail" may give official aggregate counts. Either could serve as a reference
  showing how much of the market the sample covers. Neither was inspected in this review. **Verify**
  before any design depends on them. Until then, no insight claims a market share.

#### Salary comparability (new in v3)

**Verified facts. [V]**
- **Code:**
  - `Job` stores `salary_min`, `salary_max`, `salary_currency` only. There is no period, no gross/net
    basis and no working time (§1.6).
  - The France Travail collector maps no salary.
  - Indeed keeps no pay interval.
  - `matches_salary` compares values of unknown period directly (§1.6).
- **Real data, real-v3, 1,161 FT offers (one user, IT-biased sample):**
  - `salaire.libelle` is present on 477. By period: Annuel 376, Mensuel 83, Horaire 18.
  - All 477 match the pattern `^(Annuel|Mensuel|Horaire) de <n> Euros( à <n> Euros)?`.
  - 71 state a single value. 21 are ranges whose high end is more than 1.5× the low end.
  - Period × contract: Annuel/CDI 318, Mensuel/CDI 49, Annuel/MIS 39, Mensuel/CDD 20, Annuel/CDD 19,
    Mensuel/MIS 13, Horaire/MIS 11, Horaire/CDI 5, Horaire/CDD 2, Mensuel/DIN 1.
  - **Gross or net is almost never stated** in the `salaire` object: "brut" 2, "net" 1, neither 561
    (of 564 non-empty objects).
  - "sur 13 mois" appears 3 times and "sur 14 mois" 3 times.
  - `commentaire` holds free text such as "Selon profil", "A définir", "13ème mois", or a restated
    annual range.
  - `complement1`/`complement2` hold benefits (meal vouchers, bonuses, profit sharing), not amounts.
  - Working time: `dureeTravailLibelleConverti` is "Temps plein" 293, "Temps partiel" 6, absent 862.
    `dureeTravailLibelle` is present on 393.
  - `typeContrat`: CDI 852, MIS (temporary work) 194, CDD 101, LIB (freelance) 13, DIN 1.
    `alternance = true` on 31.

**Assumptions. [A]**
- France Travail amounts are **gross** (brut) by convention. Not stated in the data. Verify in France
  Travail's API documentation or offer-publication rules. Until verified, FT amounts have
  `basis = unknown`, with `basis_presumed = gross` recorded separately.
- The FT `libelle` pattern holds for other occupations and users. Verify on the developer corpus;
  unmatched strings are counted, never guessed.
- French full-time is 35 h/week, which is 151.67 h/month. This is the legal duration, not re-verified
  here. Collective agreements and executive day-count contracts (*forfait jours*) differ.
- Other sources (HelloWork, Indeed, JSON-LD `baseSalary` on career sites) state period and basis
  inconsistently. Not measured in this review.

**Normalized record. [D]** A derived `salary_obs` record per offer. It never overwrites the offer's
raw fields. Fields:
- `low`, `high`: as stated;
- `kind`: fixed, range, from ("à partir de"), up_to, or not_stated ("selon profil", "à définir");
- `currency`;
- `period`: hour, day, week, month, year, or unknown;
- `basis`: gross, net, or unknown;
- `basis_presumed`;
- `months_per_year`: 12 unless "sur 13/14 mois" is stated;
- `working_time`: full_time, part_time, or unknown;
- `weekly_hours`, if stated;
- `contract_stratum`: permanent/fixed-term employee (CDI, CDD), temporary work (MIS), freelance (LIB,
  day rate), apprenticeship (alternance), internship;
- `parse_status`, plus `exclusion_reason` when relevant;
- `source_field`, `parser_version`.

**Conversion rules. [D]** Only these conversions are allowed:

| From | To annual full-time equivalent | Condition |
|---|---|---|
| year | as is | — |
| month | × `months_per_year` | the month count is stated or defaults to 12. The default is recorded so "12 vs 13 months" stays visible |
| hour | **not converted by default.** Hourly amounts stay in a separate **hourly stratum**, with statistics in €/h (mostly MIS in the sample) | **Conversion with stated hours:** only when the offer states full-time **and** weekly hours. Then × stated hours × 52, labelled "converti à partir de N h/semaine indiquées dans l'offre". **Illustrative conversion:** when working time or hours are unknown, a value may appear **only as an individual, explicitly labelled illustration**: "équivalent annuel indicatif, en supposant un temps plein de 35 h — hypothèse non indiquée dans l'offre". It never enters a statistic, a comparability class, a salary check (AD-2b) or a market claim |
| hour, part-time | stays in the hourly stratum | no annualization, illustrative or otherwise |
| day (freelance day rate) | **never converted** | a day rate excludes social charges and idle days. It is not comparable with a salary |
| week | **never converted** | rare; ambiguous |
| part-time (monthly or annual amounts) | × (35 / weekly hours) | only when weekly hours are stated. Otherwise **excluded** from full-time-equivalent comparisons |

**Exclusion and separation rules. [D]**
- **Gross and net are never converted into each other.** The ratio depends on status (executive or
  not, public or private) and on the year. A comparison uses one basis only. Values with
  `basis = unknown` are compared only with other `unknown` values from the **same source**, and are
  labelled "base (brut/net) non précisée".
- **Never pool contract strata.** Apprenticeship and internship pay is regulated, so it is excluded
  from market comparisons. Temporary work, freelance and employee strata are shown separately.
- **Ranges:** low ends and high ends are summarised separately (p25/p50/p75 of each). A midpoint is
  used only when `high / low ≤ 1.5`. Wider ranges contribute to the low-end and high-end summaries,
  never to a midpoint. "From" values count only in low-end summaries, and "up to" values only in
  high-end summaries.
- **Not stated / unparseable:** excluded, with a reason code.
- **Implausible values:** an annual full-time-equivalent value outside [P_min, P_max] is excluded and
  logged. P_min and P_max are parameters, unapproved. They catch unit errors, such as a monthly
  amount labelled annual.
- **Currency:** non-EUR values are excluded from French comparisons. No exchange-rate conversion.
- **Benefits and variable pay** (complements, "13ème mois" mentioned only in comments) are recorded
  as flags. They are never added to amounts.

**Comparability class. [D]** A market salary statistic is computed only inside one class: (currency,
basis, normalized period, contract stratum, working-time treatment), plus the AD-8 occupation × area
× period cell.
- The descriptor shows the class, n inside it, and n excluded by reason. Example: "Salaire annuel
  brut présumé, CDI/CDD temps plein — 212 offres ; exclues : 18 horaires, 6 temps partiel sans
  durée, 3 fourchettes incohérentes."
- **No statistic mixes classes.**
- **No comparison between a profile and the market** unless the profile's expectation is in the
  same class. Today the profile minimum has no period, basis or working-time treatment, so this
  needs a profile contract change (§8 item 7, decided).
- **Profile salary contract (v5, owner decision §8 item 7). [D]** The future profile salary
  expectation carries:
  - the amount and the **currency**;
  - the **period** (annual or monthly; hourly only as its own stratum);
  - the **basis** (gross or net);
  - the **working-time treatment**: full-time, or part-time with stated weekly hours, so a part-time
    expectation is compared only with offers whose hours are stated (AD-8 conversion rules);
  - for a monthly period, the months per year when the user states it (12 by default, shown).
  It lands only after the `matches_salary` correction (§8 item 8).
- The Market-indication level also requires n ≥ 30 **inside the class**.

**Existing filter. [V]** `matches_salary` already compares values of unknown period, for example a
monthly offer amount against an annual profile minimum. **Implementation prerequisite (v4)**, not
fixed by this document:
- change the salary check to compare `salary_obs` within one comparability class;
- return UNKNOWN for incomparable values (AD-2b "Salary check").

This must land before any salary-based filtering, the profile salary contract change, or any new
collector salary mapping.

#### Schema delta [D]

- `salary_obs` (derived table, keyed by `job_id`, rebuildable; added to cleanup `DERIVED_TABLES`).
- `market_build` gains `coverage_json` (the descriptor) and `scope_hash`, a hash of the collection
  configuration.
- Aggregate rows carry `n`, `source_mix_json`, and `scope_hash`.
- A change of `scope_hash` between periods breaks trend comparisons automatically.

#### Success criteria [D]

- UI test: every market figure renders its descriptor. No figure is rendered with n below the level's
  minimum.
- Salary advice is absent when the Market-indication conditions fail (fixture test).
- Trend statements are suppressed across a scope change (fixture test).
- Byte-identical rebuild; nightly build ≤ 60 s for 100k offers (unapproved targets, as v1).

### 4.9 AD-9: Self-monitoring — **Approve direction**

Unchanged. All thresholds unapproved. Additions **[D]**:
- **exposure logging health:** visible exposures per session; share of sessions with zero logged
  exposures while lists were viewed (it detects observer breakage);
- **AD-5 status metrics:** days in shadow, sessions, positives, discordant count, posterior P. Shown on
  Quality, never as a request to the user;
- validity rules (AD-2a) are monitored separately from user-constraint checks.

### 4.10 AD-10: Labels — **Approved**

Unchanged.

### 4.11 AD-11: Lanes — **Approve direction**

Unchanged. Additions **[D]**:
- hidden offers (AD-2c) are excluded from every lane;
- the "Reconversion possible" entry rule follows AD-7.4.

Lane sizes and the automatic shrink rule are unapproved.

### 4.12 AD-12: Privacy, retention and erasure — **Resolved**

#### Four user actions, with exactly what each one keeps

| | **Pause learning** | **Reset learning** | **Erase my activity** | **Delete all application data** |
|---|---|---|---|---|
| Nature | reversible switch | non-destructive marker | destructive, personal activity | destructive, everything |
| `feedback_events` (explicit, behaviour, saved/hidden, outcomes) | kept, used | kept; events before the `learning_reset` marker are **ignored by learning** | **deleted** (application tracker: see below) | deleted |
| Hidden offers stay hidden | yes | **yes**: hide is list management, not learning | no (the hide events are deleted) | — |
| Application tracker (outcome statuses) | kept | kept, still shown | deleted, **unless** the user ticks "Conserver mon suivi de candidatures" | deleted |
| `exposure_log` | kept | kept; ignored before the marker | deleted | deleted |
| `personal_model_state`, `ranking_shadow`, `personal_health`, `shadow_positive` (v5) | kept | **cleared** (derived; rebuilt from post-marker signals) | deleted | deleted |
| `personalization_score_cache`, legacy `job_feedback`, legacy `outcome_events` | kept | cleared | deleted | deleted |
| `job_scores.personal_*` columns | kept | kept as history only; **never read for ranking** after the reset (AD-3 "Reset and stored personal scores") | **set to NULL** | deleted |
| Disagreement items holding a user stance (ADR 0010) | kept | kept | stance fields deleted | deleted |
| Calibration labels (Quality › Calibration) | kept | kept; tuning answers ignored before the marker | deleted (option shown, ticked by default) | deleted |
| Flow-4 ops metrics (derived from signals) | kept | kept | **deleted** | deleted |
| Other `ops_*` (runs, sources, extraction) | kept | kept | kept (not personal) | deleted |
| `market_*` | kept | kept | kept (built from collected offers, never from actions) | deleted |
| Offers, decisions, provenance | kept | kept | kept. Offers lose cleanup protection and are removed by normal cleanup when expired | deleted |
| Profile and config | kept | kept | kept | deleted (back to the setup wizard) |
| Application backups (`backups/jobs-*.db`) | — | — | **all pre-erasure backups deleted** | deleted |
| Calibration annotation backups (`annotations.NNN.csv`) | — | — | deleted with the calibration labels | deleted |
| Profile backups (next to the profile) | — | — | kept (profile, not activity) | deleted |
| Frontend outbox and local storage (AD-6) | — | — | entries of the old generation are rejected (409) and dropped; cleared on completion | cleared |
| `lab.db` and `data/lab/` (developer tooling, if present) | kept | kept | **kept by default.** Optional, separately confirmed lab purge (see "Lab data during erasure") | deleted |
| Logs (`logs/`) | — | — | kept (must hold no personal content, see below) | deleted |

**[A]** The table lists every place where personal activity is stored. Verify at implementation with a
test that enumerates the database tables and fails on any table not classified in this table. The
classification test is part of the definition of done.

#### Erasure procedure: persistent and resumable (rewritten in v3) [D]

**Why v2 was not enough. [V]** (by construction) v2's database transaction is atomic, but the whole
procedure is not:
- deleting backup files happens outside SQLite, after the commit;
- a crash between the commit and the backup deletion leaves personal data in `backups/`;
- in WAL mode, a crash between the commit and the checkpoint can leave old page images in the main
  database file. **[A]** This is standard SQLite WAL behaviour, not re-tested here.

v2 also did not say what happens when the app restarts mid-procedure.

**Principle.** Erasure is a **job with a persistent state machine**. Every step is idempotent and
safe to repeat. The UI shows "Effacement en cours" or "Effacement à terminer" until the final
verification passes. It shows "Effacement terminé" only then.

**Persistent state (content-free).**
- **Sidecar marker file** `<data folder>/ERASURE_PENDING`, written and fsynced **before** anything is
  touched. It holds only the `erasure_id` and the requested scope. It survives even if the database
  cannot be opened.
- **Table `erasure_jobs`** (`erasure_id`, `requested_at`, `scope_json`, `state`, `attempts`,
  `last_error`, `inventory_json`, `verification_json`, `completed_at`). It holds no personal content:
  - `inventory_json` lists table names, row counts and file paths;
  - a **verification sample** of up to 50 erased `event_id` values (opaque UUIDs) is kept until
    verification, then deleted.

**Erasure generation (new in v4).**
- **[V]** v3 relied on comparing `occurred_at` with the erasure time. The server stamps `occurred_at`
  on receipt (§1.6), so a replayed old request would receive a fresh timestamp and pass that check.
  Client-supplied timestamps would be no better: they are under client control.
- **[D]** The server keeps an integer `erasure_generation` in a one-row `app_state` table in
  `jobs.db`.
  - It starts at 0.
  - It is incremented **inside the DB_ERASED transaction**, so the increment commits atomically with
    the deletion.
  - It is never set or proposed by a client.
- **[D] Distribution.** The current generation is returned by the session endpoint the frontend calls
  at load. It is also returned in a response header (`X-Erasure-Generation`) on every API response.
  The frontend stamps each outbox entry and exposure batch with the generation it held **when the
  user acted** (AD-6), not when it sends.
- **[D] Enforcement.** Every personal write (durable events, best-effort events, exposure batches,
  calibration answers, status changes) must carry `generation`.
  - The server compares it with the current value **inside the insert transaction**.
  - A mismatch is rejected with 409 `erasure_generation_mismatch`. Best-effort batches are dropped
    silently.
  - A missing generation is rejected for durable events. Best-effort batches without it are dropped.
- **[D] Stored per row.** `feedback_events.generation` (AD-6 schema) and `exposure_log.generation`
  record the generation a row was written under.
- **[D] Server-side jobs.** A job that writes personal rows (outcome recording, any future learning
  job) reads the generation when it starts. It re-checks the generation in its write transaction and
  aborts if it changed. The pipeline is stopped during an erasure anyway (QUIESCED).
- **[D] Reset learning** does not change the generation. Reset is non-destructive and uses its own
  marker.

**States.**

```text
REQUESTED → QUIESCED → INVENTORIED → DB_ERASED → WAL_TRUNCATED → COPIES_REMOVED → VERIFIED → COMPLETE
                  ↘ BLOCKED(reason)   (any step; retried at each start and on "Réessayer")
```

| Step | Action | Idempotent because | Crash here leaves |
|---|---|---|---|
| REQUESTED | write the marker (fsync); insert the `erasure_jobs` row | the marker is rewritten with the same id | no change yet; resume restarts the step |
| QUIESCED | stop the scheduler and pipeline; API rejects feedback, exposure and event writes (HTTP 423) and the frontend shows the banner; **`backup_database` refuses while a marker exists**, so cleanup and migrations cannot create a new copy | re-applying the locks is harmless | locks re-applied on restart **before** migrations or the scheduler start |
| INVENTORIED | list rows per table in scope; list files (`backups/jobs-*.db`, annotation backups when in scope, known app-written temp files); store counts, paths and the event-id sample | the inventory is recomputed and merged | nothing deleted |
| DB_ERASED | one transaction: `secure_delete=ON`, drop the append-only triggers, delete or NULL in-scope data, recreate the triggers, **increment `erasure_generation`**, write the content-free erasure record to `erasure_jobs` (v5: **no** marker event in `feedback_events`), **set state = DB_ERASED in the same transaction** | the state and the deletion commit together; rerunning deletes nothing more | either nothing erased (rollback) or erased with the state recorded |
| WAL_TRUNCATED | `PRAGMA wal_checkpoint(TRUNCATE)` with other connections closed; **check the busy flag is 0** and the `-wal` size is 0; retry with backoff; then `VACUUM` when free space allows | checkpointing twice is harmless | the WAL is checkpointed on resume |
| COPIES_REMOVED | delete every inventoried file created before `requested_at`; re-list the folders and delete any match the inventory missed | deleting a missing file is a no-op | the remaining files are deleted on resume |
| VERIFIED | checks (below); on failure → BLOCKED with the failing check | checks are read-only | rechecked on resume |
| COMPLETE | delete the event-id sample; set state COMPLETE; delete the marker (fsync the folder); lift the locks; the frontend receives the new generation, drops old-generation outbox entries and clears local storage; show success with counts | — | if the marker deletion fails, resume finds state COMPLETE and only removes the marker |

**Erasure record (new in v5).** v4 appended a content-free `erasure` marker event to
`feedback_events`. That contradicted verification item 1, which requires zero rows in that table.
v5 keeps the record in control tables only:
- `erasure_jobs` holds `erasure_id`, `requested_at`, scope, generation before and after, row counts
  per table, and the state. It holds no personal content;
- `app_state` holds the current generation (and, since v5, the data epoch; see "Delete all
  application data");
- both are **control tables**. They are excluded from the in-scope list and classified as such in
  the table classification test;
- learning needs no marker event: the generation and the empty tables already mark the cut.

**Verification (all must pass before success is shown):**
1. row counts in in-scope tables are 0, `feedback_events` included, except the tracker or calibration
   data the user chose to keep. Control tables (`erasure_jobs`, `app_state`) are not in scope;
2. the `-wal` file is absent or 0 bytes after the checkpoint;
3. a byte search of the database file **and** the `-wal` file finds none of the sampled `event_id`
   values. Because of `secure_delete`, freed pages are zeroed, so a hit means a copy survived;
4. no file in the inventoried folders matches a backup or annotation-backup pattern with a
   modification time before `requested_at`;
5. **generation check:**
   - `app_state.erasure_generation` equals the value recorded at DB_ERASED;
   - no row in a personal table carries an older generation, except the categories the user chose to
     keep (tracker, calibration);
   - a probe write stamped with the previous generation is rejected. The probe is a dry-run check
     inside a rolled-back transaction.

   Old outbox entries or queued beacons therefore cannot re-insert erased activity, whatever their
   timestamps.

**VACUUM** is attempted, but it is not a verification condition. It needs about the database size
in free space. **[V]** (by construction) After `secure_delete` and the checkpoint, check 3 confirms
the sampled rows no longer exist in either file. When VACUUM is skipped for lack of space, the result
says so ("fichier non compacté").

**BLOCKED cases and messages.**
- A backup file is locked (OneDrive sync, antivirus): the message names the file and says
  "Réessayer". Retries happen at each start.
- The checkpoint stays busy: retried; the message asks the user to close other app windows.
- The database cannot be opened: the marker keeps the erasure pending. The message offers "Supprimer
  toutes les données de l'application", which deletes the files.

The app never reports success in a BLOCKED state. The pipeline stays stopped while an erasure is
pending, so no new activity mixes with old.

**Startup order.** Check the marker and `erasure_jobs` **first**, before migrations
(`backup_before_migrating` could otherwise copy the database) and before the scheduler. **[V]**
`backup_before_migrating` copies a database ≥ 200 MB during migrations.

**"Delete all application data" (rewritten in v5).** v4 used the same state machine, but its
resume state (`erasure_jobs`) lives in the database being deleted. After the database file is gone,
a crash would leave nothing to say which step was reached, what remained, or how to verify. v5
gives Delete-all its own **database-free** procedure.

- **External state file.** `DELETE_ALL_PENDING.json`, in the data folder's **parent**, outside
  everything being deleted. It is written atomically (temporary file, fsync, rename, fsync of the
  folder) **before** anything is touched, and rewritten the same way at each step. Content-free:
  - `erasure_id`, `requested_at`, the absolute path of the data folder;
  - the step reached and the attempt count, and the last error;
  - the **inventory**: every path to delete (data folder tree, backups, lab files, profile and
    config files in scope) with its size, and no content;
  - the per-check verification results.
- **States.** REQUESTED → QUIESCED (as for Erase, plus the lab writers' suspension below) →
  INVENTORIED (the inventory is written to the external file) → FILES_DELETED → VERIFIED →
  COMPLETE. Every step reads only the external file and the filesystem, never the database.
- **Resume.** At startup the external file is checked **first**, before the database is opened or
  created. While it exists, the app does not create a new database and starts nothing else. It
  resumes at the recorded step.
- **Verification without the database:**
  1. every inventoried path is absent;
  2. the data folder is absent, or holds only files created after `requested_at` by this procedure;
  3. no file in the data folder's parent or `backups/` matches a database, WAL, backup or lab
     pattern with a modification time before `requested_at`;
  4. no lab writer recreated a file during the procedure (lab suspension still in force until
     this check passes).
- **COMPLETE.** Delete the external file (fsync the parent folder). On next start, the setup wizard
  runs and a new database is created.
- **Data epoch.** A new database starts at generation 0, so an old outbox entry stamped with
  generation 0 would match. To prevent this, every new database writes a random `data_epoch` UUID
  in `app_state`. The `X-Erasure-Generation` header and every personal write carry
  `<data_epoch>:<generation>`. An entry from a deleted database has the wrong epoch and is rejected
  with 409 whatever its generation. The frontend also clears its outbox and local storage when it
  sees a new epoch.
- **Locked file.** If a file cannot be deleted (OneDrive, antivirus, a lab process), the state is
  BLOCKED with the file named. The external file keeps the procedure pending; retries happen at
  each start and on "Réessayer".

**Tests [D]:**
- kill the process after each state (fault injection at each transition) and assert that resume
  reaches COMPLETE and verification passes;
- assert that the UI shows no success before VERIFIED;
- assert that `backup_database` refuses while the marker exists;
- the table classification test (from v2), with `erasure_jobs` and `app_state` classified as
  control tables;
- **(v5)** Delete-all: kill the process after each step, including after the database is deleted,
  and assert that resume completes from `DELETE_ALL_PENDING.json` alone;
- **(v5)** an outbox entry from a deleted database (old epoch, generation 0) is rejected by the new
  database;
- **(v5)** lab purge: a lab writer started during the purge fails to open or create `lab.db`; a
  non-default active strategy in either location blocks the purge; a held-open `lab.db` gives
  BLOCKED, not success.

#### Limits stated to the user (and in the docs) [D]

- Copies outside the application's control are **not** erased: files the user exported; Windows File
  History or OneDrive copies of the data folder; system restore points; antivirus quarantine.
- Deleted files and freed pages may remain physically on the disk (especially on SSDs) until the
  operating system overwrites them. The application cannot guarantee physical wiping.
- The `-shm` file is SQLite's WAL index and holds no row content. **[A]** This is standard SQLite
  behaviour, not re-verified for this review.

#### Retention defaults [D] (unapproved values)

| Data | Default retention |
|---|---|
| `feedback_events` | until the user erases it (it is the user's history and tracker) |
| `exposure_log` | 180 days rolling. Also: rows older than 30 days are compacted to (job, list, first visible date, best position); `visible_ms` details are dropped |
| `ranking_shadow` | 90 days |
| `personal_health`, `ops_*` | 400 days |
| `market_*` | aggregates kept; periods retained indefinitely (no offer text, no personal input) |
| Application backups | as today (2 kept), deleted by erasure |

#### Other rules

- **[D]** Logs must not contain notes, reasons, preference vectors, profile text or per-offer event
  lists. **[A]** Current logs comply. Verify with a log lint test over a full pipeline run and a UI
  session before L1 ships.
- **[D]** Export (Settings › Data) includes learned preferences in readable form (portability).
- **[D]** INV-2 socket-guard test as in v1.
- **Lab data during erasure (new in v4; replaces the v3 sentence that follows). [D]**
  - **What lab.db holds [V]** (§1.6): engine-plane records. These are hypotheses, candidate bundles,
    immutable experiments, approvals, lab-agent memory and reports, and the append-only
    `active_strategy` pointer the pipeline honours.
  - **[A]** Experiment results, memory items and agent reports may contain per-offer content derived
    from the user's own labels. `result_json`, `details_json` and `report_json` are free-form, and
    their content on a real install was not inspected. Verify by reading the lab writers before the
    AD-12 ADR is finalised.
  - **"Erase my activity" does not touch `lab.db` or `data/lab/` by default**, for two reasons:
    - deleting `active_strategy` would silently change production behaviour, or leave it pointing
      at nothing;
    - lab records are the audit trail for engine changes (ADR 0011).
  - **When `lab.db` or `data/lab/` exists**, the erase dialog says so in plain words. It states that
    these developer files may contain results derived from the user's labels and are not erased by
    this action. It then offers a separate, explicitly confirmed **"Effacer aussi les données du
    laboratoire"**.
  - **Lab purge rules** (when chosen; rewritten in v5):
    1. **Both locations.** **[V]** (§1.6) `data/lab.db` and `data/lab/lab.db` can both exist, and the
       API reads only one of them. The purge therefore inventories and checks **both**.
    2. **Default strategy in both.** The purge is allowed only when **each** existing `lab.db`
       shows production on the **shipped default strategy**: no active pointer, or the last pointer
       action is `deactivate` or a rollback to the default. If either file shows another active
       strategy, or cannot be read, the purge is BLOCKED ("désactivez d'abord la stratégie de
       laboratoire active"). The purge can never change what production does.
    3. **Suspend the lab writers first (at QUIESCED).**
       - The app writes a content-free suspension marker `LAB_SUSPENDED` in `data/lab/` and in
         `data/`. **[V]** (§1.6) The isolated lab agent's account may write `data/lab/`; **[A]** it
         can read a marker there. Verify with the lab isolation check before the ADR.
       - Every lab writer opens `lab.db` through one function. That function refuses to open, and
         refuses to **create**, a lab database while a marker exists. Lab writers: the lab agent,
         the lab CLI (`python -m src.lab`), the promotion tool, and the `/api/lab/*` write routes
         (which return 423).
       - The app stops the lab processes it started itself. It cannot stop a lab agent running
         under another OS account (ADR 0011).
    4. **Prove no writer is active.** For each existing `lab.db`, the app takes an exclusive SQLite
       lock (`BEGIN EXCLUSIVE`, with a timeout), releases it, and deletes the file. **[A]** On
       Windows a file held open by another process cannot be deleted, so a running lab agent makes
       the deletion fail. That failure is BLOCKED with "un processus du laboratoire est actif ;
       arrêtez-le puis réessayez", never a partial success. **[A]** The app's account may lack the
       right to delete files the lab account created in `data/lab/`; that case is also BLOCKED with
       the file named.
    5. **Delete as files.** It deletes both `lab.db` files (with `-wal` and `-shm`) and the contents
       of `data/lab/`, **except** the suspension marker. No rows are deleted, so the immutability
       triggers are untouched.
    6. **No recreation.** The markers stay in place until VERIFIED re-checks that no lab file
       reappeared in either location after deletion. They are removed at COMPLETE. After that, the
       lab tooling may create an empty database on its next use.
    7. It is part of the same state machine. The files are inventoried at INVENTORIED, deleted at
       COPIES_REMOVED, and their absence is checked at VERIFIED.
  - **"Delete all application data"** always removes the lab files, in both locations. It uses the
    same suspension (rules 3, 4 and 6), without the default-strategy check, because production is
    removed too.
  - **Dialog (owner decision, §8 item 13):** the lab purge option is shown only when a lab file
    exists in either location.
- **[D]** Labelling workspaces **outside** the application's data folder (developer evaluation
  folders) are outside every in-app action, including "Delete all". The app did not create them in
  its data folder and does not track them. `lab.db` and `data/lab/` inside the data folder follow the
  lab rule above.

**Conflicts.** ADR 0009 ("erasure is a deliberate owner-side DB operation") is amended for the public
app, as in v1.

### 4.13 AD-13: Roadmap — **Approve direction**

Updated stages in §6.

---

## 5. Dependencies (updated)

```text
AD-12 resolved (generation, state machine) ──► AD-6 (durable outbox + generation stamping; visible exposure; Save, Hide)
                                                   │
                                                   ├──► AD-3 (model, shadow only)
                                                   └──► AD-5 surfaces + attribution ──► AD-5 simulator (G1–G3, G3b params)
                                                                                          ──► AD-5 activation ──► personal ranking on
matches_salary correction (prerequisite) ──► AD-2b salary check ──► profile salary contract (§8 item 7)
                                         └─► any collector salary mapping (incl. France Travail)
AD-8 salary_obs ──► AD-2b salary check (comparability classes)
AD-7 D-design / D-calib / D-test corpus ──► AD-7 thresholds and calibration tables ──► AD-7.5 domain gate
AD-1 pack (ROME first) ──► AD-7 (prototype classifier; three concepts) ──► AD-8 (coverage-scoped market)
                       └── ESCO only after licence + size verified (§1.5)
AD-2a validity ── no new dependency (existing code paths, journaled separately)
AD-2b weak evidence ── pre-registration + lab; interim proximity floor until AD-7
AD-3.2 estimated position correction ── needs AD-6 logs + randomized exploration; its own release
AD-9 additions ── need AD-6 (exposure health) and AD-5 (status)
AD-12 lab purge ── needs the lab writers' content check (§4.12 [A])
(v5) AD-12 lab purge ── needs the single lab-DB opener honouring LAB_SUSPENDED (lab tooling change)
(v5) AD-12 data epoch ──► AD-6 generation stamping (<epoch>:<generation>)
(v5) AD-6 ordering (client_seq, superseded) + timestamps (effective time)
        ──► AD-3 recency and aggregation order
        ──► AD-5 run association (shadow_positive) and 14-day windows
(v5) AD-5 S4 exploration pool ── needs AD-2a validity + STRONG constraints only; drawn before S1 ranking
(v5) AD-5 SR-1…SR-5 simulator scenarios ──► n_LB,min, c_contra, c_het, m, cap_exp ──► EXPERIMENTAL / ACTIVE
(v5) AD-7 D-calib-A ──► D-calib-B (offsets and temperature before isotonic and thresholds)
(v5) matches_salary correction ──► profile salary contract (period, basis, working time, currency)
```

---

## 6. Stages (updated)

All exit thresholds are proposals, to be confirmed when each stage is planned.

| Stage | Contents | Exit criteria |
|---|---|---|
| L0 Decisions | ADRs for approved ADs, written **after** this review is accepted | merged; no code |
| L1 Signals and privacy | AD-6 visible-exposure logging, Save/Hide (AD-2c semantics), AD-12 four actions, table classification test, log lint, socket guard | AD-6 and AD-12 criteria; erase verification test passes on a temporary DB |
| L2 Structured evidence + ops base | FT ROME, experience and education into typed evidence; extractor vs structured; source and field detectors | as v1 |
| L3 Model in shadow + simulator | AD-3 computed and journaled, not shown; AD-5 simulator | INV-7 bit identity; offline learning curve; simulator false activation ≤ 5 % |
| L4 Personal ranking eligible | AD-5 activation rule live; panel; health monitor | fault-injection pause works. Activation itself is **not** an exit criterion: it happens only when the evidence exists |
| L5 Weak evidence | as v1 | as v1 |
| L6 Occupations | ROME pack; ESCO gate check; prototype classifier; three concepts; wording tests | AD-7 criteria |
| L7 Market | coverage descriptor; three claim levels | AD-8 criteria |
| L8 Full self-monitoring | as v1 plus AD-9 additions | AD-9 criteria |
| L9 (optional) Estimated position correction | AD-3.2 | offline validation on logged data |

The order stays sequential, per the owner's preference.

---

## 7. Failure modes (delta from v1 §10)

| Failure | Detection | Safeguard | Rollback |
|---|---|---|---|
| Personalization activated on noise | simulator before ship; posterior P logged | G1–G3; clustering cap | pause; release rollback |
| Personalization never activates for most users | AD-9 status metric across testers | profile suggestions and hide work without activation | revisit G2 via the simulator, never G3 |
| "Ignored" over-penalises | — | inert at launch; per-offer cap; never the embedding | keep it disabled |
| Observer breaks and logs nothing | AD-9 exposure health | AD-5 G1 not met, so no activation | fix; the ranking stays production |
| Hide over-generalises | user unhides; pattern prompt dismissed | no generalisation by default; pattern weight bounded | remove the pattern contribution |
| Skill wording implies a lack | UI string test | INV-9 | — |
| Classifier slow or memory-heavy | run duration share; memory report | new offers only; float16; fewer prototypes | `domain_basis: keywords` |
| Market sample presented as the market | UI descriptor test | three claim levels; scope flags | hide the level |
| Erasure incomplete | verification step + classification test | atomic transaction; secure_delete; checkpoint; backup purge | rerun erase (idempotent) |
| Erasure interrupted | `ERASURE_PENDING` marker + `erasure_jobs.state` (v3) | DB step atomic; every other step idempotent; success only after VERIFIED | automatic resume at next start |
| ESCO licence problem found late | pack manifest licence field | ESCO optional | ship a ROME-only pack |
| Crash mid-erasure leaves copies | `ERASURE_PENDING` marker; `erasure_jobs.state` | persistent state machine; startup check before migrations; `backup_database` refuses during erasure | automatic resume; BLOCKED message with a retry |
| Queued or replayed event re-inserts erased data | server erasure generation (v4) | generation checked in the insert transaction; 409 on mismatch; generation verification at VERIFIED | — |
| Explicit event lost (page closed, server crash) | outbox age notice | IndexedDB outbox; acknowledgement after commit; retry | redelivery at next start |
| Retried event duplicated | unique `client_event_id` | idempotent insert | — |
| Lab purge changes production | active-pointer check | purge blocked unless the default strategy is active | — |
| Challenger wins by self-reinforcement on production surfaces | G3b(b) stratum disagreement; attribution sensitivity report | G3b; strict LB attribution | pause; release rollback |
| G3b vacuous because LB volume is low | LB counts on the Quality page | (v5) ACTIVE requires LB ≥ n_LB,min; otherwise SHADOW, or EXPERIMENTAL if enabled (§8 items 11, 14) | return to SHADOW |
| AD-7 thresholds tuned on test data | D-test usage log; fresh D-test rule | four-way data separation | re-evaluate on a fresh D-test |
| Hourly pay annualized into statistics | comparability-class check | hourly stratum; illustration-only labelled conversion | suppress the statistic |
| Salary check rejects on incomparable values | AD-2b salary fixture tests | UNKNOWN for incomparable values; prerequisite ordering | per-check flag |
| Challenger activated on production-shaped evidence | per-surface report | G3b on the less-biased stratum | pause; release rollback |
| Posterior read as causal proof | wording review; UI string test | wording rule | — |
| Classifier favours codes with many prototypes | accuracy by prototype-count bucket | per-code scoring; prototype cap; offset; calibration | group fallback for the affected domaine |
| Confusion across distant occupations | per-domaine confusion report | abstain on a cross-domaine margin | group fallback |
| Salary statistic mixes incompatible values | comparability-class check in the market build | class rules; exclusions with reasons | suppress the statistic |
| FT basis presumed gross turns out wrong | documentation check (§8) | `basis = unknown` until verified | relabel; rebuild `salary_obs` |
| **(v5)** S3 lane order inflates the challenger's evidence | per-stratum report (IM) | S3 moved to IM; never used by G3b | — |
| **(v5)** Exploration pool depends on production | S4 seed, pool size and π logged per run | pool built from validity and STRONG constraints only; drawn before S1 | — |
| **(v5)** Vacuous G3b read as validation | `activation_state` in `personal_health`; UI label | ACTIVE needs LB ≥ n_LB,min; EXPERIMENTAL labelled unconfirmed | return to SHADOW |
| **(v5)** EXPERIMENTAL inflated by its own exposure | SR-5 simulator scenario | PF and IM positives during EXPERIMENTAL excluded from G3/G3b; small cap; 60-day timeout | return to SHADOW |
| **(v5)** Self-reinforcing challenger passes the independent-outcome test | SR-1 and SR-2 scenarios | gates set on the worst grid point | pause; release rollback |
| **(v5)** Positive counted twice or moved to a later run | `shadow_positive` primary key on `job_id`; write-once row | deterministic anchor rule | — |
| **(v5)** hide then unhide ends hidden | ordering test | FIFO per offer; `client_seq`; `superseded` at insert | user unhides again |
| **(v5)** Two browsers send contradictory actions | — (documented limit) | server insert order decides | user corrects |
| **(v5)** Wrong client clock distorts recency or windows | `client_time_trusted = 0` share | trust window; fallback to receipt time | — |
| **(v5)** Lost `viewed` read as a negative | test | `viewed` never required; absence neutral | — |
| **(v5)** Delete-all crash after the DB is gone | `DELETE_ALL_PENDING.json` | DB-free state machine; startup check before DB creation | automatic resume |
| **(v5)** Old outbox entry accepted by a recreated DB | epoch mismatch (409) | `data_epoch` in every personal write | — |
| **(v5)** Erasure record left in an in-scope table | verification item 1 | record kept in control tables only | — |
| **(v5)** Lab writer recreates files during the purge | VERIFIED re-check in both locations | `LAB_SUSPENDED` marker; single lab-DB opener refuses to create | BLOCKED; rerun |
| **(v5)** Active lab strategy in the other `lab.db` | both-locations check | purge blocked unless both show the default | — |
| **(v5)** Pre-reset personal scores reused | bit-identity test after reset | readers filter by reset marker and model version | — |
| **(v5)** AD-7 offsets or thresholds over-fitted on D-calib | D-test calibration error | D-calib-A / D-calib-B or cross-fitting; pooled offsets | re-fit; fresh D-test |
| **(v5)** On-install accuracy inflated by same-employer offers | — | held-back part drawn by employer; near-duplicates removed | — |

---

## 8. Owner decisions (recorded in v5) and remaining open items

**Decided by the owner in the fourth review** (each is now part of the design):

| # | Topic | Decision | Where applied |
|---|---|---|---|
| 1 | Low-activity users | Accepted: ranking personalization may never activate for them. Profile suggestions and hide still work | AD-5 "Accepted consequence" |
| 2 | Pre-erasure backup | **No** backup before an erasure | AD-12 |
| 3 | Application tracker under "Erase my activity" | Deleted by default, with an explicit "keep my tracker" option | AD-12 four-actions table |
| 4 | ESCO in the first pack | **No.** ROME first | AD-1, AD-7.1 |
| 5 | "Ignored" signals | Logged but inert at launch | AD-3.1, AD-6 |
| 6 | Causal evaluation | Not now. Revisit after L4 | AD-5 |
| 7 | Profile salary contract | **Yes**, with period, basis (gross/net) and working-time treatment, plus currency | AD-8 "Profile salary contract" |
| 8 | `matches_salary` correction | Priority, before any new salary filtering or mapping | AD-2b, AD-8 |
| 9 | Writes during erasure | HTTP 423, no queuing | AD-6, AD-12 |
| 10 | Lab writers' content | Verify before finalizing the AD-12 ADR (ADR 2) | AD-12 |
| 11 | Thin LB evidence | Unconfirmed activation is never presented as a normal validation | AD-5 "Activation states" |
| 12 | Outbox without IndexedDB | Memory fallback with a visible warning; never claims durability | AD-6 protocol step 7 |
| 13 | Lab purge option | Shown only when lab files exist | AD-12 lab rules |

**Open verifications** (not decisions; each must be done before the affected ADR or implementation,
as stated in §10):
- France Travail salary basis (gross?) — before AD-8 salary statistics;
- the per-code appellation distribution in the ROME export — before AD-7 implementation;
- the ROME licence PDF — before AD-7 implementation;
- the ESCO legal notice and package size — only if ESCO is reconsidered;
- machine-readable mobility links — only if mobility is reconsidered;
- whether lab writers store label-derived per-offer content (item 10) — before ADR 2 is finalized;
- **(v5)** whether the isolated lab agent can read `LAB_SUSPENDED` in `data/lab/`, and whether the
  app's account can delete files the lab account created there — before ADR 2 is finalized.

**Still open (new in v5):**

14. **EXPERIMENTAL state at launch.** Ship the EXPERIMENTAL state enabled, or ship with ACTIVE only
    (a minimum LB volume, otherwise SHADOW)? Recommended: **ship it disabled**, and enable it only
    after the SR-4 and SR-5 simulator scenarios pass. This is the more conservative reading of item
    11. It can be settled inside ADR 6 without changing its structure.
15. **Two browser profiles on one install.** Accept server insert order for cross-profile
    conflicts (recommended, documented limit)? Or forbid durable actions from a second profile
    while another holds unacknowledged entries?
16. **S4 without a score floor.** Accept that some "Découverte" offers are poor matches
    (recommended; it is the price of independence)? Or allow a floor computed from a
    production-independent signal only (for example, occupation-group match), recorded in π?

---

## 9. Open parameters (all unapproved)

- **AD-2b:** lane size 10; score floor 60; proximity floor 0.6; penalties 0.10 / 0.25; age ramp 1× to
  3×; hide pattern rule (3 in 30 days; −0.2).
- **AD-3:** signal weights; recency half-life 90 days; profile-change discount 0.5; `cap(n)` formula;
  β, α_e, κ; exploration 2 of 20; diversity 6 / 3.
- **AD-5:** 14 days; 10 sessions; 20 positives; 12 discordant; ≥ 3 positions; 14-day positive window;
  clustering cap 3 per session; P ≥ 0.95 to activate; 8 discordant and P ≥ 0.80 to halve; top-20 drop
  50 %.
- **AD-6:** 50 % visible for 1 s; engaged-session definition; ignored after 3 engaged sessions;
  candidate weight −0.05, cap −0.10; beacon every 5 s.
- **AD-7 (v4, matches the v3/v4 method):**
  - prototypes: M = 3 appellation prototypes per code; L = 3 local prototypes per code; a local
    minimum of 5 offers before local prototypes are added; per-code score = mean of the top-min(2,
    n) similarities; softmax over the top 10 codes;
  - fitted on D-calib, not set in advance: temperature T, count offset, isotonic tables (code, dp,
    gd), thresholds τ_code, δ, τ_dp, τ_gd;
  - calibration per grand domaine when it has ≥ 200 D-calib offers;
  - on-install held-back share of local FT offers: 20 %;
  - near-duplicate cut across splits: cosine 0.95;
  - domain-gate calibrated probability 0.8; similarity bands; run-time budget 5 %.
- **AD-8:** n ≥ 5 / 20 / 30; field coverage ≥ 50 %; co-occurrence support 20.
- **AD-12:** retention values; exposure compaction at 30 days; verification sample of 50 event ids;
  checkpoint retry and backoff.
- **v3 additions:**
  - AD-5 (v4): G3b n_LB,min, c_contra, c_het, margin m (replace v3's "5 discordant / mean ≥ 0.5");
    14-day `surfaces_seen` window; attribution-gap warning threshold.
  - AD-6 (v4): retry backoff schedule; unacknowledged-outbox notice delay.
  - AD-12 (v4): none new (the generation is a counter, not a tunable).
  - AD-7: see the AD-7 line above (v4 replaces this entry).
  - AD-8: range midpoint ratio 1.5; plausibility bounds P_min and P_max; class minimum n ≥ 30.
- **v5 additions:**
  - AD-5: n_LB,min (now a requirement for ACTIVE); `cap_exp` ≤ 25 % of `cap(n)`; EXPERIMENTAL
    maximum displacement 3 positions and protected top 3; EXPERIMENTAL halving at ≥ 6 discordant and
    P(worse) ≥ 0.70; 60-day EXPERIMENTAL timeout; S4 no-repeat window 14 days; simulator criteria
    (ACTIVE ≤ 5 %, EXPERIMENTAL ≤ 10 %, G3b blocking ≥ 80 %, EXPERIMENTAL exit ≥ 90 %); ρ grid
    {0, 0.2, 0.5}.
  - AD-6: client-time max_lag 30 days; skew 5 minutes.
  - AD-7: n_offset,min for a code's own offset; cross-fitting k = 5.
  - AD-12: none tunable (data epoch and suspension markers are mechanisms, not parameters).

---

## 10. Readiness for ADR drafting

**Recommendation: ready for sequential ADR drafting, with the conditions below.** v5 closes the four
design problems the owner found in v4 (AD-5, AD-6, and the two AD-12 points) and adds the requested
clarifications. **This is a documented architecture, not a validated one.** No experimental result
was produced for v5; every threshold stays unapproved until the simulator, the developer corpus or a
release measures it.

**Readiness per ADR (v5):**

| # | ADR | Status after v5 | Conditions |
|---|---|---|---|
| 1 | Planes and invariants | ready | — |
| 2 | AD-12 Privacy, retention and erasure | design closed (Delete-all external state, data epoch, erasure record in control tables, lab suspension and both locations) | **before finalizing:** the lab-writer content check and the two lab-permission checks (§8 open verifications) |
| 3 | AD-6 Signal capture and delivery | design closed (ordering per offer, two timestamps, `viewed` neutral) | ADR 2 (generation and epoch); §8 item 15 can be recorded inside the ADR |
| 4 | AD-2 Validity, user constraints, hide, salary UNKNOWN | ready | `matches_salary` correction kept as an implementation prerequisite |
| 5 | AD-3 Personal preference model | ready (Reset clarification added) | ADR 3 |
| 6 | AD-5 Shadow evaluation and health monitor | design closed (three strata, independent S4 pool, activation states, self-reinforcement scenarios, run association) | ADR 3, 5; §8 items 14 and 16 can be recorded inside the ADR |
| 7 | AD-4 Promotion regimes (+ ADR 0011 amendment) | after ADR 6 | ADR 6 |
| 8 | AD-10 Labels as developer instruments | ready | — |
| 9 | AD-1 Knowledge packs | ready | licence checks as implementation preconditions |
| 10 | AD-7 Occupation model | possible | ADR 9; ROME licence PDF and appellation distribution as implementation preconditions |
| 11 | AD-8 Market plane and salary comparability | possible | ADR 10; FT basis as implementation precondition |
| 12 | AD-9 Self-monitoring | after dependencies | ADR 6, 10 |
| 13 | AD-11 Lanes | after dependencies | ADR 4, 10 |
| 14 | AD-13 Roadmap update | last | all above |

**Remaining blockers (v5):**
- **Blocks finalizing ADR 2 (not drafting it):** the lab-writer content check; whether the isolated
  lab agent can read the suspension marker; whether the app's account can delete files the lab
  account created. Each changes wording or adds a BLOCKED case, not the structure.
- **Decisions that can be settled inside their ADR:** §8 items 14 (ADR 6), 15 (ADR 3), 16 (ADR 6).
- **Blocks implementation, not ADRs:**
  - AD-5 activation: the SR-1…SR-5 simulator results, which set n_LB,min, c_contra, c_het, m and
    `cap_exp`;
  - AD-7: the ROME licence PDF, the per-code appellation distribution, and the developer corpus
    with D-design / D-calib-A / D-calib-B / D-test;
  - AD-8 salary statistics and AD-2b salary filtering: the France Travail salary basis, the
    `matches_salary` correction (priority), then the profile salary contract.
- **Not blocking any ADR:** ESCO, the estimated position correction, causal evaluation (deferred by
  owner decision).

**Final ADR drafting order** (unchanged from v4; sequential, one ADR per PR, never in parallel):
1 Planes and invariants → 2 AD-12 → 3 AD-6 → 4 AD-2 → 5 AD-3 → 6 AD-5 → 7 AD-4 → 8 AD-10 → 9 AD-1 →
10 AD-7 → 11 AD-8 → 12 AD-9 → 13 AD-11 → 14 AD-13.

Planes and invariants come first because every other ADR cites them. AD-12 comes before AD-6
because delivery depends on the erasure generation and the data epoch. AD-6 comes before AD-3 and
AD-5 because both read its ordering and effective-time rules.

## Sources

Official pages (consulted 2026-11-05):
- data.gouv.fr — ROME dataset (licence, counts, update dates):
  https://www.data.gouv.fr/datasets/repertoire-operationnel-des-metiers-et-des-emplois-rome
- France Travail open data — ROME page (formats, correspondence tables):
  https://www.francetravail.org/opendata/repertoire-operationnel-des-meti.html?type=article
- data.gouv.fr — API ROME 4.0 dataservice (licence "non renseignée"):
  https://www.data.gouv.fr/dataservices/api-repertoire-operationnel-des-metiers-et-des-emplois-rome-4-0
- francetravail.io — API ROME 4.0 announcement: https://francetravail.io/blog/api-rome-4
- ESCO — Other crosswalks (O*NET, NACE only): https://esco.ec.europa.eu/en/use-esco/other-crosswalks
- ESCO — Download (v1.2.1 counts; no licence or size stated): https://esco.ec.europa.eu/en/use-esco/download
- Commission Decision 2011/833/EU on reuse of Commission documents: https://eur-lex.europa.eu/eli/dec/2011/833/oj
- data.europa.eu copyright notice: https://data.europa.eu/copyright-notice

Third party (not authoritative):
- Mindmatcher — ROME → ESCO alignment report: https://mindmatcher.org/blog/alignement-rome-vers-esco

Research (evaluation):
- Radlinski, Kurup, Joachims (2008), *How Does Clickthrough Data Reflect Retrieval Quality?*
  (team-draft interleaving). Cited as background, not re-read in this review.

Research (position bias):
- Joachims, Swaminathan, Schnabel (2017), *Unbiased Learning-to-Rank with Biased Feedback*:
  https://arxiv.org/abs/1608.04468
- Agarwal et al. (2019), *Estimating Position Bias without Intrusive Interventions*:
  https://arxiv.org/abs/1812.09338

Local evidence (read-only):
- public `main` `6207a7c` source files cited inline;
- `<owner labelling folder>/data/real-v3/jobs.db` (1,161 FT offers; salary, contract, working-time
  and appellation counts in v3 measured read-only);
- synthetic vote-bias illustration: a local simulation script (not committed; synthetic data only)
  (random data; not validation);
- `<owner labelling folder>/data/lab/decision-2026-11-03/REPORT.md` and `FILTER-INVESTIGATION.md`.
