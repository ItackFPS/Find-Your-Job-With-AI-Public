# 2.0 completion plan: audit corrections, review decisions and execution order

Status: **approved by the owner on 2026-10-30.** Written after a full audit of `main` `8b30e37`.

The approved plan `2026-10-23-final-update-plan.md` **remains the reference** for scope and
acceptance criteria; `2026-10-23-roadmap-status.md` is the execution record. This document adds
what the audit found and what the owner decided on review, and fixes the execution order.
Tags: **[plan]** original requirement (item id); **[audit]** correction found by the audit
(D1–D15); **[review]** owner decision of 2026-10-30 (R-…). PR numbers prefixed "private" refer to
draft PRs of the former private development repository, to be ported selectively.

Audit evidence on `8b30e37`: `pytest -m "not network"` 5268 passed, 9 skipped, 0 failed; synthetic
self-check PASS (0 unsafe rejections, precision 0.88, recall 1.0, unchanged); on the owner's
`real-v1` labels (250, 96 % assisted, 10 accept), deterministic precision 0.058 and 0 unsafe
rejections once freshness-only rejections (offers already older than 30 days at export) are set
apart. Ranking quality is not measured on `main`.

## R. Owner review decisions (2026-10-30)

| Id | Decision |
|---|---|
| R-Q1 | `real-v1/annotations.csv` → `labels.jsonl` (250 labels) is the existing labelled dataset; **never ask for them to be relabelled**. Preserve the original files. Look for any other Excel copy before treating the CSV as sole source (search result in §E). The assisted-label limitation stays visible in every evaluation. |
| R-Q2 | Merge PR #70 after review, checks green on current public `main`, and self-review. |
| R-Q3 | Daily background collection **on by default for official/public sources**, configurable in the app: disable it, see when each source runs, its status and errors, and run scans manually. Unofficial sources stay explicit opt-in with clear explanations and controls. |
| R-Q4 | Embedding model downloaded on first run, with visible progress, SHA-256 verification, retry, and understandable errors; installer kept small. |
| R-Q5 | Label the first 100 of `real-v2` through the app: a proper, simple calibration experience, expandable to all 310 or more if 100 is not enough. |
| R-Q6 | Usability, hardware and remote-label criteria stay **not met** until actually done and verified. |
| R-Q7 | Keep the private repo until useful evaluation work is ported **and verified** on public `main`. Port #130–#134 **selectively**, never blind-merge; lose no history or personal data. Then archive, never delete. |
| R-Q8 | The reordered roadmap is approved: measurement before intelligence changes; real interfaces for existing backend features. |
| R-Q9 | Clean obsolete worktrees only after inspecting each for unique commits, uncommitted changes and useful work. Never delete because something looks old. |
| R-C1 | **Matching quality:** do not optimise for rejection rate or one precision figure. Goal: find genuinely relevant offers and rank them well, keeping the conservative eligibility philosophy. Use real-v2 blind labels, ranking metrics, confidence intervals and held-out evaluation; investigate false positives by category and source. The 5.8 % precision is a warning, not a reason to redesign the engine. |
| R-C2 | **UX in every phase:** each user-facing feature ships with a usable interface when it lands. No more backend-only features. Phase 6 consolidates and polishes; it is not the first time the app becomes usable. |
| R-C3 | **AI setup:** the app works fully without AI. Detection, hardware assessment, recommendations, installation, progress, benchmark and troubleshooting are all in the UI, as shared components used by both onboarding and Settings. |
| R-C4 | **Release quality:** 2.0 is tested as a real application from a fresh install: migration, background operation, source consent, usability, accessibility, hardware compatibility, error recovery and a complete user journey. |
| R-A1 | **Plan approved** (2026-10-30). Execute autonomously, phase by phase, sequentially, keeping docs, tests, status record and this handoff updated. |
| R-A2 | **Live database:** do not choose it arbitrarily. Identify the database actually opened by the app used daily and **confirm its exact path with the owner** before preparing migrations (P1.4) or labelling (P2.3). |
| R-A3 | **Release testing:** usability test with 3 non-technical users is **release-blocking**; hardware: 3 machines targeted, 2 acceptable if a third is hard to get (reported as such); 100 remote labels stay not met until labelled. |
| R-A4 | **Porting:** do not just reuse old code: verify that each ported metric and sampling method is valid for the current pipeline (distance model, career levels, EQF education, new strata). |
| R-A5 | **First 100 labels:** a representative selection across real-v2's strata (random control, near threshold, per source, top-scored, pipeline-rejected), not only top-ranked offers; the sampling design and weights are stated in every report. |
| R-C5 | **Vision:** a polished, intelligent job-search app that discovers opportunities automatically, evaluates them transparently, ranks the most relevant, and helps the user act; understandable to non-technical users, with no terminal commands, JSON edits or AI-model knowledge needed. |

Execution rules carried over: K1 (agent squash-merges green, self-reviewed PRs; a code-review pass
for contract or security PRs; releases, tags and repo settings need the owner), sequential work
(one item at a time, finished, reviewed and merged before the next), CLAUDE.md §11 workflow
(issue → branch → small green commits → draft PR → CI → self-review → merge), and the plan's
invariants (ADR 0004 safety, unsafe-rejection gate, journals, loopback-only AI, lab isolation,
human promotion).

---

## D. Corrections found by the audit [audit]

| # | Type | Finding | Evidence | Correction |
|---|---|---|---|---|
| D1 | Missing | No background run of the main pipeline: only `linkedin_daily`/`hellowork_daily` exist; FT web, EURES and remote boards run on "Run" or after a LinkedIn/HelloWork scan | `config/schedules.yaml`; `src/scheduler/service.py:619` | `pipeline_daily` schedule (R-Q3) with UI |
| D2 | Missing | Backend features without UI: AI setup, source health/breakers, unofficial consent | no frontend consumer of `/api/ai/*`, `/api/sources/*` | Settings page + shared components (R-C2, R-C3) |
| D3 | Architecture | Two profile models side by side (scoring, filter, explain, FT department selection, personalization branch on `home`/`targets`) | `scoring.py:1057`, `filter.py:228`, `orchestrator.py:499`, `Profile.tsx` | v2-only UI and defaults; v1 paths removed one release after 2.0 |
| D4 | Missing | Ranking quality unmeasured on `main`; precision on real labels 0.058 | §E | Port ranking metrics; held-out real-v2 labels (R-C1) |
| D5 | Architecture | No expiry/provenance; `jobs` table never shrinks (owner DB 3.6 GB) | no expiry fields | F6.6 + retention/compaction |
| D6 | Methodology | real-v1 includes offers already > 30 days old at export; 3 accept labels count as "unsafe" only through freshness | real-v1 reasons table | Report freshness-only rejections separately; future exports keep only offers fresh at `as_of`. Never "fix" the filter for this |
| D7 | Architecture | Backend returns English sentences, not codes | `src/pipeline/explain.py` | New features emit codes from day one; legacy prose converted with the Offer page |
| D8 | Missing | Embedding model fetched from Hugging Face on first use, silently | `src/embeddings/onnx_encoder.py` | R-Q4 first-run download UI |
| D9 | Cleanup | n8n/Discord leftovers: lab relay port 5678, comments in `origin_guard.py`, `peer_guard.py`, `app.py:295`, `docs/development.md:509-534`, `config/scoring.yaml:26`; unused notification tables | grep | One cleanup PR; drop tables in 2.1 |
| D10 | Architecture | Ad-hoc schema evolution in ~12 modules | `src/storage/sqlite.py:168-180` | Small central ordered migration list, schema version, pre-migration backup (refactor PR first, no behaviour change) |
| D11 | Dormant | ATS collectors ship with no boards; no Ashby collector in public | `config/sources.yaml` | Leave for F6.7 |
| D12 | Security | CORS Vite origin when `API_SAME_USER_ONLY` off; installer pins without hashes; EURES `Crawl-delay: 10`; backend `apply_url` scheme validation; retire `scripts/install_startup.py` | status record follow-ups | One hardening PR before release |
| D13 | Process | Stale status record; 11 stale private drafts; ~100 local folders | §0 | Phase 0 |
| D14 | Process | Version still `1.0.0` | `pyproject.toml` | `2.0.0.dev0` |
| D15 | Architecture | Disagreement engine can't flag rejections offline | real-v1 `--assess` | Stay in shadow until AI evidence exists; measure capture rate before enabling review |

No rewrite is recommended: the deterministic core, journals and safety gates are sound and tested.
The gaps are wiring, a half-finished migration, missing interfaces and missing measurement.

---

## P. The final plan

### P.1 Cross-cutting rules (apply to every phase)

1. **Definition of done for a user-facing item [review R-C2, R-C5]:** a non-technical user can use
   it from the app: no terminal, no JSON or YAML edit, no AI-model knowledge; EN + FR strings; empty,
   loading and error states with a recovery action; keyboard-reachable; and its tests. Backend-only
   PRs are allowed only as the first slice of an item whose UI slice follows next.
2. **New code returns codes + parameters, never prose** for anything shown to users [audit D7].
3. **Matching changes [review R-C1]:** judged on held-out blind labels with ranking metrics and
   CIs; never accepted for raising rejections; 0 new unsafe rejections on synthetic and personal
   sets; promoted only through the lab with human approval.
4. **Evaluation honesty [review R-Q1, R-Q6]:** every report states label provenance (assisted vs
   independent), sample sizes, CIs and "insufficient evidence"; unmet criteria are reported as not met.
5. **Data safety:** personal data never enters the public repo; DB migrations back up first and are
   rehearsed on a copy of the owner's database; original dataset files are never modified.
6. **Records:** after each merge, update `docs/plans/2026-10-23-roadmap-status.md` (status, evidence,
   next actions) and the PR body; this document is updated at each phase end.

### P.2 Phases

Each phase lists: objective · already exists · to do (tagged) · acceptance · risks · postponed.

#### Phase 0 — Truth and housekeeping

- **Objective:** an accurate record and a clean, safe base.
- **To do:**
  - P0.1 [audit D13] Status record updated: #61–#68, #70, 2026-10-26 decisions, §C classifications,
    §R decisions, this plan as the execution order (docs PR).
  - P0.2 [review R-Q2] Review #70, re-run checks on current `main`, self-review, merge.
  - P0.3 [audit D14] Version `2.0.0.dev0`.
  - P0.4 [review R-Q9] Worktree inventory: for each of the ~100 local worktree folders, record
    branch, unique commits not on any remote, uncommitted changes, and verdict (keep / delete /
    rescue). Delete only folders with nothing unique; list the rest for you. `data/` folders inside
    worktrees (throwaway test DBs) are listed, not deleted, unless clearly test copies.
  - P0.5 [audit D9] n8n/Discord leftover cleanup PR (comments, relay port 5678, docs).
- **Acceptance:** status record lists every merged PR and decision; #70 merged with green CI;
  inventory file committed to the audit folder; nothing unique deleted.

#### Phase 1 — Background collection, with its interface

- **Objective:** with the window closed, enabled sources collect daily; the user sees and controls
  everything about sources from the app.
- **Exists:** tray agent, scheduler with checkpoints, #70, breakers, canary, FT web collector.
- **To do:**
  - P1.1 [audit D1, review R-Q3] `pipeline_daily` schedule, on by default for official/public
    sources; catch-up of at most one missed run; no burst.
  - P1.2 [plan F6.5, F1.3, F10.3; review R-Q3, R-C2] **Settings › Sources and background** page:
    - background collection on/off, next and last run per source, manual "scan now";
    - per-source status, last error in plain words, breaker state and reset;
    - unofficial sources (LinkedIn, Indeed, HelloWork): off unless chosen, one-sentence risk text,
      consent recorded with a date;
    - components built to be reused by the wizard (P3).
  - P1.3 [audit D10] Migration registry refactor (no behaviour change, own PR).
  - P1.4 [plan F6.6, audit D5] Expiry (validThrough, N consecutive misses, user report) and
    cross-source provenance merge (FT web/API/EURES shown once, "seen on …" in the UI); retention
    and on-demand compaction of the `jobs` table from Settings › Data. Contract change → its own plan.
  - P1.5 [audit D12] EURES crawl-delay and the other source/security follow-ups.
- **Acceptance:** fresh install, unofficial off, window closed 24 h → one run happened, FT web
  offers present, visible in Settings with time and status; a 429 pauses and resumes without burst;
  a disabled background stops scheduled runs; consent off by default and stored when given; a
  duplicate across FT web + EURES appears once with 2 provenances; migration rehearsed on a copy of
  the owner's large database (several GB) with a backup.
- **Risks:** site blocking (opt-in, breakers); DB migration time on large DBs (measured on the copy).
- **Postponed:** F6.7 ATS auto-follow, F6.9 URL paste.
- **Status (2026-10-30): merged.** P1.1 #77, #79, #81; P1.2 #83; P1.3 #85; P1.4 #87, #88, #90, #89;
  P1.5 #91; review fixes #92. Against the acceptance criteria:

  | Criterion | Status |
  |---|---|
  | one background run with the window closed, FT web offers, visible in Settings | mechanism tested; live: a fresh copy of `main` ran the daily search by itself once the profile was saved (8.4 min, 691 offers, FT web present) and Settings showed time, status and next run; the **24 h window-closed run is not done** |
  | a 429 pauses and resumes without burst | tested (#70); after a startup catch-up the new period's run can follow within the hour (follow-up) |
  | a disabled background stops scheduled runs | tested (#81); Settings says so (#92) |
  | consent off by default, stored with its date | tested (#83); a manual scan no longer counts as a choice (#92) |
  | FT web + EURES duplicate shown once with 2 provenances | tested on fixtures (#87); not observed in a live run |
  | migration rehearsed on a copy of the large DB with a backup | **done**: copy of the 3.5 GB DB, migrations + backup 19 s; cleanup 71 s (copy, delete 14,704, VACUUM), integrity ok |

  Found during P1 and fixed: expiry counted truncated listings as absence (#90); cleanup had no
  backup and could run beside a manual scan (#89); dev origins allowed in installed apps (#91);
  manual scans turned daily unofficial scans on (#92); an offline daily run counted as done (#92).

#### Phase 2 — Measurement foundation

- **Objective:** a trustworthy, repeatable answer to "does it find and rank relevant offers?", in
  the Quality page.
- **Exists:** runner with frozen clock and bootstrap CIs, self-check, Quality page, real-v1, real-v2.
- **To do:**
  - P2.1 [review R-Q7] Port from the private repo, one public PR each, rebuilt on current code
    (§P.4).
  - P2.2 [review R-C1] **Pre-register before labelling:** freeze the 2.0 candidate changes to be
    tested (re-expressed for the distance model; #134's H1/H2 kept as history) and the decision
    rule, with hashes, before any real-v2 label exists.
  - P2.3 [plan F0.2, review R-Q5] **Calibration experience** in Quality: import `real-v2` from its
    folder (read-only, originals untouched); sessions of 10–30 offers; blind (no machine proposal
    or score shown); answers relevant / not relevant / not sure + optional reason category; progress
    saved; "label 100 more" to extend to 310 and to new exports; labels written as a new dataset
    version with `label_source: human`.
  - P2.4 [review R-C1] **Held-out split** fixed before use (e.g. 30 % of real-v2 labels never used
    for tuning, chosen by hash), stated in every report.
  - P2.5 [plan F0.2, audit D6] Self-check and Quality page report the personal sets: real-v1 as an
    FP/safety set with its assisted and survival-bias caveats and freshness-only rejections shown
    apart; real-v2 for ranking (AUC, precision@Strong, recall@20 with CIs) and false negatives (the
    pipeline-rejected stratum).
  - P2.6 [review R-C1] FP analysis by category and by source, as a Quality report.
  - P2.7 [plan F0.3] FT web coverage over 7 days (runs in the background from P1).
- **Acceptance:** 100 blind real-v2 labels exist (the owner's action); Quality shows ranking metrics with
  CIs on held-out labels, label provenance, and "insufficient evidence" where true; v2-profile
  baseline recorded (the owner's profile migrated on a copy).
- **Risks:** labelling fatigue (short sessions); leakage of personal data (anonymised exports,
  `test_no_personal_profile_in_tests.py`).

#### Phase 3 — Profile v2, AI setup and onboarding, each with its UI

- **Objective:** a non-technical user sets up everything, with or without AI.
- **To do:**
  - P3.1 [audit D3] Example profile ships v2; Profile page rebuilt on v2 sections (About me,
    Experience, Education, What I want, Where); v1 editors removed from the UI (fields kept
    read-only for one release, then removed).
  - P3.2 [plan F8.1–F8.3, review R-C3] **AI setup components** shared by Settings › AI and the wizard:
    detected runtimes, hardware summary in plain words, recommended model with size and why, consent
    before any download, install/pull progress with cancel/retry, benchmark verdict in words,
    troubleshooting (runtime not running, not enough memory, too slow, port blocked) and "continue
    without AI".
  - P3.3 [review R-Q4, audit D8] **Embedding model first-run download** in the wizard and Settings:
    size shown, progress, SHA-256 check, resume/retry, clear errors (offline, proxy, disk full);
    the app keeps working (deterministic ranking) while it is missing.
  - P3.4 [plan F5.4] Wizard: language → CV or questions → targets → place → sources (P1 components)
    → AI (P3.2) → background → first search with live progress.
  - P3.5 [plan F10.3] Settings › Language, Data (export, delete, erase CV/documents, compaction).
  - P3.6 [plan F5.5, T] CV import (rules first, AI when present), each field showing its source line.
- **Acceptance:** fresh install → first results with no file edit; works with AI absent and with
  AI configured; migration tests on 3 fixture profiles + a copy of the owner's profile with the review screen;
  strategy-version re-record in its own PR.

#### Phase 4 — Ranking quality on real offers [review R-C1]

- **Objective:** better ranking of genuinely relevant offers without weakening eligibility safety.
- **To do:** test the pre-registered candidates on held-out real-v2; investigate FPs by category
  and source; improvements through ranking (weights, distance decay, domain cap, semantic
  matching), not through new hard rejections; personalization measured on held-out labels before
  being on by default (plan C10); F7.3 posting language (flag, never drop) with a filter in Jobs.
- **Acceptance:** each change promoted via the lab with human approval; held-out ranking metrics
  improve beyond the CI or the change is not promoted; 0 new unsafe rejections (synthetic + personal).

#### Phase 5 — AI as analyst, in the Offer page

- **To do:** [plan F8.4] offer analysis with exact-substring quotes and profile-entry references,
  queue ordered by score within a time budget plus on demand, results linked to decisions;
  shown as a fit map and action plan on the Offer page (codes, not prose); retire Phase-2 top-100
  enrichment; [audit D15] feed AI evidence to the disagreement engine and measure review capture on
  labels before enabling review. Then T items: F8.5 tailored CV/letter (DOCX + print-to-PDF, fact
  checker, user edits before export), F8.6 discovery with yield tracking, F8.7 translation with
  cache and original always visible.
- **Acceptance:** grounding ≥ 95 %; 0 rejections caused by AI; latency recorded; works and says so
  when AI is absent; 20-offer document review with 0 unsupported claims (T).

#### Phase 6 — Consolidation and polish [review R-C2]

- **To do:** Home (Strong / Worth a look / Needs your judgement, background chips, expiring
  offers); Jobs (filters, saved views, compare); Applications tracker; Pipeline/Stats/Skills merged
  into Insights and Quality › Runs; F9.2 disagreement browser, F9.3 strategy comparison; remaining
  prose → codes; F7.2 EN+FR complete, ES/IT/DE beta; consistent states everywhere; accessibility
  pass.
- **Acceptance:** pseudo-locale clean; axe-core 0 serious; keyboard-only journey; i18n missing keys 0.

#### Phase 7 — Release qualification as a real application [review R-C4]

- **To do:** F11.1 slim installer + components manifest; release test matrix run on the built
  installer, from a fresh Windows account and from an upgrade of v1.0.0:
  fresh install · upgrade with profile and DB migration · first-run embedding download (online,
  offline, interrupted) · wizard without AI and with AI · background operation (window closed 24 h,
  reboot, crash, quit leaves nothing) · source consent and a blocked source · error recovery
  (no network, disk full, port taken, runtime missing) · accessibility · hardware compatibility
  (machines listed in §P.5) · usability test · complete journey (install → set up → results →
  understand an offer → act → feedback → Quality). §H report from the plan with every criterion
  met or explicitly not met; release only with the owner's approval.

### P.3 Dependencies and critical path

```
P0 ─▶ P1 (background + Sources UI) ─▶ P2 (measurement) ─▶ P4 (ranking quality)
            │                              ▲                        │
            └──▶ P3 (profile v2, AI setup, wizard) ──────────────────┤
                                                                     ▼
                                              P5 (AI analysis) ─▶ P6 (polish) ─▶ P7 (release)
```

- **Critical path:** P0.2 → P1.1 → P1.2 → P1.4 → P2.1 → P2.2 → P2.3 → *the owner's 100 labels* → P2.5 → P4 → P7.
  The owner's labelling is the one external dependency on it; P3 runs while labels are being collected.
- P1 before P2: labels and coverage need offers that arrive by themselves.
- P2.2 before P2.3: candidates must be frozen before any label exists.
- P3 before P5: AI analysis needs the AI setup UI and the v2 profile it maps requirements to.
- P1.3 (migration registry) before P1.4 and any later Job-model change.
- Sequential execution: one item in flight; P3 work fills the time while you label (no parallel agents).

### P.4 Work to port from the private repository [review R-Q7]

Port **selectively**, as new public PRs rebuilt on current code (no cherry-pick of old SHAs, no
blind merge); each verified by tests on public `main` before the private PR is closed with a link.

| Private PR | Content | Verdict | Notes |
|---|---|---|---|
| private #130 `feat/evaluation-ranking-metrics` | AUC/ranking of accepted jobs, borderline sensitivity, ΔAUC on a common cohort, ranking change beside lab verdicts (+271 lines) | **Port** (P2.1, first) | adapt to the distance-model factors |
| private #133 `feat/evaluation-sampling-cohorts` | uniform sampling cohorts beside pooled metrics; reads strata from the private sidecar (+`sampling.py`) | **Port** after #130 | needed to read real-v2's strata correctly |
| private #131 `feat/export-top-scored-stratum` | blind top-scored stratum in export + audit replay | **Port** | real-v2 already contains this stratum: public `audit` must replay it, and extending to new exports needs it |
| private #132 `fix/export-rejected-pool-sequential-scan` | sequential journal scan for the rejected sample (performance on large DBs) | **Port** | small; matters for large databases |
| private #129 `fix/lab-approval-shows-change` | approver sees the actual change; runnable approve command | **Port** | safety of human approval |
| private #134 `docs/lab-preregistered-candidates` | H1/H2 frozen 2026-10-17 with hashes and decision rule | **Port as history**, re-freeze 2.0 equivalents (P2.2) | written against the v1 location factor and the `fyj-lab-proposal/1` format that public lacks |
| private #126 / #128 proposal inbox, LLM proposer | untrusted LLM writes a validated proposal | **Defer** (optional) | only if a tool-using lab agent is wanted |
| private #123–#125 lab launcher, AppContainer, bootstrap hardening | | **Do not port** | superseded by public #30 |

Also preserved, not ported: the private `data/` folder (the owner's database, real-v1, real-v2, backups),
which the calibration flow reads in place or from a copy. After all ports are verified, the
private repo is archived on GitHub (read-only), never deleted.

### P.5 Release-blocking acceptance criteria for 2.0

Original plan criteria (§H and tier R items) unless tagged. Each is reported met or **not met**.

| Area | Criterion | Origin |
|---|---|---|
| Safety | 0 unsafe rejections on synthetic (CI) and on personal sets (freshness-only reported apart) | [plan §H], [audit D6] |
| Ranking | precision@Strong and recall@20 on held-out blind real-v2 labels, with CIs; no promoted change worsens them beyond the CI | [plan §H], [review R-C1] |
| Labels | ≥ 100 blind real-v2 labels collected in the app; provenance shown in every report | [review R-Q5, R-Q1] |
| Remote | 0 restricted-elsewhere offers typed `remote_france`; 100 owner-labelled remote postings ≥ 90 % correct | [plan F4.2] — stays not met until done (R-Q6) |
| Fresh-install yield | no credentials, unofficial off → FT web offers present; opted in → LinkedIn, Indeed, HelloWork present | [plan §H] |
| Background | daily runs with the window closed; disable works; per-source next/last run, status and errors visible; reboot resumes; Quit leaves no process | [plan §H], [review R-Q3] |
| Consent | unofficial sources off by default, explained, consent recorded and revocable in the app | [plan I1, F6.5] |
| Source robustness | a failing source never fails the run; a changed page → canary error, never a silent 0 | [plan §H] |
| No-terminal | every user-facing action (setup, sources, AI, quality, data) reachable in the UI; no JSON/YAML edit | [plan F9.1, F10.3], [review R-C2, R-C5] |
| AI optional | full journey works without AI; AI setup, benchmark and troubleshooting in the UI | [review R-C3], [plan D6] |
| AI grounding | ≥ 95 % quotes valid; 0 rejections caused by AI | [plan §H] |
| Embeddings | first-run download with progress, SHA-256, retry; clear error when offline | [review R-Q4] |
| Migration | v1.0.0 → 2.0 upgrade keeps data, profile migrated with backup and review screen, rehearsed on a large real DB copy | [plan §G], [review R-C4] |
| Startup and size | first window ≤ 3 s; backend warm ≤ 10 s, cold ≤ 25 s; installer < 100 MB, installed < 400 MB (targets, measured) | [plan §H] |
| i18n | EN + FR complete, 0 missing keys, pseudo-locale clean, explanations translated | [plan F7.2] |
| Accessibility | axe-core 0 serious; keyboard-only onboarding and apply journey; AA contrast | [plan §H] |
| Hardware | hardware probe and AI recommendation checked on 3 machines | [plan F8.1] — not met until done (R-Q6) |
| Usability | 3 non-technical users: onboarding < 10 min; find and save 3 relevant offers < 5 min, unaided | [plan §H] — not met until done (R-Q6) |
| Security | Host/Origin/loopback/traversal probes, instance token, no outbound request carrying profile data, D12 items closed | [plan §H], [audit D12] |
| Removals | no Discord/n8n/digest code or settings; leftovers cleaned | [plan F1], [audit D9] |
| Quality gates | all suites, audits and installer acceptance (extended test matrix) green on the release build | [plan §H], [review R-C4] |

### P.6 First implementation tasks (after approval)

1. **P0.1** Docs PR: status record update (+ link to this plan), record §R decisions.
2. **P0.2** PR #70: rebase/merge check on current `main`, full checks, self-review, merge.
3. **P0.3/P0.5** Version bump; n8n/Discord leftover cleanup.
4. **P0.4** Worktree inventory (read-only report first; deletions only of folders with nothing unique).
5. **P1.1** Issue + PR `feat/<n>-pipeline-daily-schedule`.
6. **P1.2** Issue + PR Settings › Sources and background (EN+FR, tests).
7. **P1.3** Plan + refactor PR: migration registry; then **P1.4** plan + PRs for expiry/provenance.
8. **P2.1** Port #130, then #133, #131, #132, #129.

---

## I. Definition of the finished product

A French job seeker downloads one installer (size and SHA-256 on the release page) and installs it
without admin rights. A window opens in English or French within seconds. A short wizard imports
their CV or asks a few questions, sets a town and a distance, explains each optional source in one
sentence and lets them choose, checks their computer and either sets up a recommended local model
with visible progress or continues without AI, downloads the matching model it needs with a
progress bar, and starts the first search.

From then on the app works from the tray: France Travail, EURES, remote boards and the chosen
sources are collected daily and politely; the user sees when each source ran, whether it worked
and why not, and can pause or run it. Duplicates appear once with all their sources; expired offers
fade away. Home shows new matches grouped *Strong / Worth a look / Needs your judgement*. A remote
job open only to US residents is never "Remote France"; on-site jobs abroad are not remote. Each
offer shows where it was found, a fit map of requirements with quoted sentences, a plain
explanation in the user's language, and, with AI, an action plan, a tailored CV and letter built
only from real profile facts, and a translation next to the original. The app never rejects a
possibly suitable offer without strong evidence and says "not sure" when unsure.

A short calibration flow and everyday feedback build a personal evaluation set. The Quality page
says, in plain words, whether matching is safe and whether a change made the ranking better, with
confidence intervals and held-out labels. Experiments run in an isolated lab; only a human promotes
them. No cloud AI, no Discord, no n8n, no digest, no terminal, no JSON. Every suite, audit and the
release test matrix pass, and every §P.5 criterion is reported as met or explicitly not met.
