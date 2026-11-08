# Release completion audit and revised roadmap (2026-11-08)

Audit of what remains to release Find Your Job With AI 2.0 as a complete, reliable application,
**without activating L3 personal ranking**. Audit and planning only: no application code,
configuration, schema, flag or ADR was changed. Earlier plans are not replaced; where this report
proposes a change to an approved decision, it says so and asks for approval (§11).

Sources of truth used: the code and tests at `main` `94578c6`, then
`2026-10-23-final-update-plan.md` (scope), `2026-10-30-completion-plan.md` (order, §P.5 release
criteria), `2026-10-23-roadmap-status.md` (status record), `2026-11-05-learning-architecture-decisions.md`
(v5, L0–L9), ADRs 0001–0027 and the reports in `docs/reports/`.

---

## 1. Executive summary

- **The engine is further along than the product.** Collection (P1), measurement (P2) and the
  learning architecture L0–L5 are merged and tested (5,657 tests pass in CI). L3/L4 run in
  SHADOW with `activation_enabled: false`; production ranking is deterministic and authoritative.
- **Nothing on the release path depends on L3 activation.** Every L3-dependent item (personal
  ranking ACTIVE, EXPERIMENTAL state, ADR 0019 revision, L9) is post-release research (§10).
- **L6–L8 are not release work.** They add occupation, market and full self-monitoring
  intelligence. Their "blockers" are partly developer tasks, not external waits (§3.3). One P7
  gate added by ADR 0027 (the multi-occupation self-check) depends on L6 and needs a decision if L6
  is deferred (§11, D2).
- **What blocks a release is the user-facing setup and the release qualification**, i.e. the
  P3, P6 (minimum) and P7 work that ADR 0027 scheduled *after* L8:
  1. setup without a terminal: Profile v2 finished, onboarding wizard, embedding download UI,
     autostart switch, France Travail credentials entry (today: CLI commands and `.env` edits);
  2. a decision on AI for 2.0: build the AI setup UI (P3.2), or ship 2.0 with user-facing AI
     hidden and move P3.2 and P5 to 2.1 (§11, D3);
  3. EN/FR completeness and an accessibility pass;
  4. P7: a 2.0 installer, the v1.0.0 → 2.0 upgrade on the built installer, the 24 h window-closed
     run, the release test matrix and the §H report;
  5. the owner-only checks: usability test (3 users), hardware check (2–3 machines), remote-type
     labels.
- **Shortest safe path** (§8): M0 records → M1 setup without terminal → M2 AI decision → M3
  minimum polish → M4 release qualification, with the owner's human checks running in parallel.
  This needs one owner decision to reorder ADR 0027 (P3/P6/P7 before L6–L8, §11 D1).

---

## 2. Verified repository state

| Item | Verified value |
|---|---|
| Public repository | `ItackFPS/Find-Your-Job-With-AI-Public` (`origin` of `FYJ-workspace/public-repo` and its worktrees) |
| Audit checkout | new worktree `FYJ-workspace/public-repo-worktrees/fyj-audit-1108`, branch `docs/release-completion-audit` from `origin/main` |
| `main` HEAD | `94578c6` "fix(api): revalidate the dashboard page, cache hashed assets for good (#155)", 2026-11-06 |
| Version | `pyproject.toml` `2.0.0.dev0` |
| Open PRs / open issues | none / none (`gh pr list`, `gh issue list`, 2026-11-08) |
| CI on `94578c6` | Tests, Lint, Frontend, Dependency audit, Secret scan: all success. Tests job: 5,657 passed, 75 skipped, 3 deselected (run 42227949169) |
| Local run (Windows) | see §2.1 |
| Private repository | not accessed. Note: the main checkout `Claude/Find-Your-Job-With-AI` is the **private** repository; it was only listed (`git remote`, `git status`), nothing read or changed in it |

Existing work preserved, not touched:

- **Uncommitted L3 research** (owner-requested evidence, not on `main`):
  `public-repo-worktrees/fyj-research-l3/docs/reports/2026-11-06-safe-passive-personalization-research.md`
  and `.../2026-11-06-l3-sequential-evaluation-correction.md` (detached worktree at `94578c6`;
  scripts and outputs in `FYJ-workspace/scratch/2026-11-06-l3-correction/`). They exist only on
  this disk. Milestone M0 proposes committing them as docs.
- 24 public worktrees under `public-repo-worktrees/` (most are squash-merged branches; R-Q9 asks for
  a per-folder inspection before any cleanup) and ~30 private worktrees under `FYJ-workspace/worktrees`.
- Labelling environment `FYJ-workspace/labelling` (never delete). Counted, not read:
  `data/calibration/real-v2/labels.jsonl` 100 lines (the owner's 100 blind labels, 2026-11-03);
  real-v3: 500 exported offers, labels file has 1 line (effectively unlabelled).

### 2.1 Tests run for this audit

| Command | Result |
|---|---|
| `python -m pytest -m "not network" -q` (Windows 11, Python 3.12.10, fresh venv, `94578c6`) | **5,697 passed, 35 skipped, 3 deselected, 0 failed** (44 min 35 s) |
| `python -m src.evaluation self-check --markdown` | **PASS**: synthetic-ai-filter-50, 0 unsafe rejections, baseline identical, precision 0.88, recall 1.0 (3 `keyword_trap` false positives, known) |
| Frontend (`npm run lint/build/test`) | not run locally (no `node_modules` in the new worktree); CI "Frontend" job green on `94578c6` |

---

## 3. L0–L9 technical status

Classification legend: **IV** implemented and validated · **II** implemented, insufficiently
validated · **PI** partially implemented · **BL** blocked by a dependency · **NI** not implemented ·
**OPT** optional/deferred.

| Layer | Class | Evidence | What is missing |
|---|---|---|---|
| L0 Decisions | IV | ADRs 0014–0027 (#111–#135) | — |
| L1 Signals and privacy | IV | exposure log, Save/Hide, outbox, socket guard, pause/reset, erase, delete-all, lab purge (#115–#128); outbox replay at load #152; delete-all auto-restart on Windows #154 (real fresh-copy test 2026-11-06); tests `test_privacy_erasure.py`, `test_privacy_learning.py`, `test_personal_data_stays_local.py` | — |
| L2 Structured evidence + ops base | IV | FT typed evidence + extractor agreement #132, Quality card #133, per-source metrics and detectors #134, Santé UI #136; `src/requirements/structured.py`, `src/ops/monitor.py` | — |
| L3 Model in shadow + simulator | IV as SHADOW | #137–#140, #143; `src/learning/{signals,model,shadow,simulator}.py`; report `docs/reports/2026-11-05-l3-simulator.md` (criteria **not met**: false activation up to 21.6 %) | nothing for release; activation research deferred (§10) |
| L4 Personal ranking eligible | IV with activation off | #141, #142, #144, #145; `config/personalization.yaml` `activation_enabled: false`; `tests/test_learning_health.py::test_release_flags_default_off`, `::test_flag_on_activates_then_fault_injection_pauses` | nothing for release |
| L5 Weak evidence | II (inert) | #146–#150; every `weak_evidence_mode` check is `reject` (`config/filters.yaml`); exploratory report: criterion 1 fails | the decisive real-v3 held-out run, which needs real-v3 labels (owner) — post-release |
| L6 Occupations | NI (BL) | no occupation pack, classifier or `targets.occupations` in `src/` (`src/esco/` is the older ESCO skills FTS, not AD-7) | ROME licence check, appellation export check, then AD-7 (§3.3) |
| L7 Market | NI (BL) | — | L6 + `matches_salary` correction + profile salary contract + FT salary basis |
| L8 Full self-monitoring | PI | source/field detectors (#134), learning health record and status (#141, #145) exist | AD-9 metrics that need L6 (classifier health) and exposure-health metrics; thresholds unapproved |
| L9 Estimated position correction | OPT | — | randomized exploration logs; only if offline validation supports it |

### 3.1 Does anything still depend on L3 activation?

| Item | Depends on activation? | Alternative without activation |
|---|---|---|
| L4 personal ranking ACTIVE / EXPERIMENTAL | yes | stays SHADOW; production ranking shown (already the behaviour) |
| ADR 0019 revision (sequential test) | yes (only purpose) | deferred; the 2026-11-06/07 research reports are kept as evidence |
| L8 AD-5 status metrics | no — they read the shadow record, which exists | ship as-is |
| L9 | indirectly (needs exploration logs) | optional, post-release |
| ADR 0026 lanes / Découverte (S4) | no — S4 is drawn independently of both rankings (#144) | ship as-is |
| P6 Home sections | no | build on deterministic tiers + existing lanes |
| P7 learning gates (ADR 0027) | no — fault-injection pause, INV-2 socket test, reset bit-identity and erasure verification all test the inert state | keep as release gates |
| Profile suggestions, Hide | no (owner decision v5 §8 item 1) | already work |

**Conclusion:** no release item depends on L3 activation.

### 3.2 L3 constraints kept by this roadmap

SHADOW stays; `activation_enabled: false`; deterministic ranking authoritative (ADR 0002); no new
activation experiment, statistical test or ADR amendment without separate approval; existing
simulator report, research reports and shadow data are preserved.

### 3.3 L6–L8 blockers, re-verified

| Blocker | Still present? | What it affects | Actually blocked by | Can proceed independently |
|---|---|---|---|---|
| ROME licence PDF | yes, not checked | L6 pack redistribution (v5 §1.1 [A]) | a **developer task**: download the PDF from data.gouv.fr and read it. No external party to wait for | yes — a one-hour docs task (needs approval for the download) |
| Per-code appellation distribution in the ROME export | yes, not checked | AD-7 classifier design | same: download the public export and count | yes, together with the licence |
| `matches_salary` correction | **yes**: `src/pipeline/filter.py:562` still compares `salary_min`/`salary_max` against the profile bounds with no period, basis or working time | L7 and any salary filtering/mapping (AD-2b, v5 §8 items 7–8); `salary` excluded from `RANKABLE_CHECKS` (`filter.py:859`) | nothing external. It needs a profile salary contract (period, basis, working time, currency) = a contract change → own plan | yes. **Current exposure is low**: `salary_requirements` is in neither the example profile nor the Profile page (`grep` finds it only in `filter.py` and `src/decisions/evidence.py`), and the FT collector maps no salary; only a hand-edited YAML triggers it |
| FT salary basis (gross/net) | yes | AD-8 statistics only | documentation check | yes |
| New blocker found | — | the ADR 0027 P7 gate "multi-occupation self-check" cannot exist without L6 | L6 | needs decision D2 |

---

## 4. P0–P7 product roadmap status

### P0 Truth and housekeeping — **done**, with drift

Done #71–#75. Drift found in `2026-10-23-roadmap-status.md`: "Next actions" still says "now L5"
(L5 merged #146–#150); the L1 row says the outbox replay is not verified (fixed by #152); the F5.1
row says the Profile page edits v1 fields only (it now edits v2 levels, education and home, see P3);
the L3/L4 rows do not record the 2026-11-08 SHADOW-final decision. M0 fixes the record.

### P1 Collection — **merged; two live criteria unverified**

| Area | State | Evidence |
|---|---|---|
| Daily background pipeline | done | #77, #79, #81, #92; `src/scheduler/service.py` |
| Settings › Sources, health, breaker, consent | done | #83, #92; `frontend/src/components/settings/SourcesCard.tsx` |
| Deduplication + provenance | done (fixtures) | #87; live FT web + EURES duplicate not yet observed |
| Expiry | done | #88, #90 (misses counted only for complete listings) |
| Cleanup/compaction | done | #89 |
| Collection with the window closed | **mechanism done, not usable without a terminal**: tray mode exists (`src/launcher/tray.py`), but start-at-sign-in is only `FindYourJob.cmd autostart on` (`docs/windows-app.md`); no UI switch (`grep autostart frontend/src` → nothing). The 24 h window-closed run was never done |
| France Travail API | needs `FRANCE_TRAVAIL_CLIENT_ID/SECRET` in `.env`; the first-run guide tells the user to set environment variables (`frontend/src/components/GettingStarted.tsx:11`). FT web works without credentials | violates the no-terminal rule (R-C5) for an optional source |
| Known follow-ups (not blockers) | per-scope expiry completeness; legacy `inactive` rows; orphan derived rows; catch-up + regular run within an hour; FT API skipped row shows a raw env-var message; flaky `test_prevents_concurrent_scans`, `test_successful_scan_triggers_pipeline` | status record "Follow-ups" |

### P2 Evaluation — **built; measured on thin evidence**

| Area | State | Evidence |
|---|---|---|
| Ranking metrics, cohorts, held-out split, CIs | done | #94, #98, #100, #102 |
| Blind calibration UI | done | #99, #106 (Quality › Calibration) |
| Pre-registration | done | #101 (H1/H2), #109 (real-v3), #146 (AD-2) |
| Lab/production separation | done, enforced by tests | `tests/test_evaluation_isolation.py`, `test_lab_guard.py`, `test_lab_promotion_authority.py`, #107 |
| Labels | real-v2: 100 blind owner labels (held-out 31, 4 accept). H1/H2 not supported (ΔAUC 0, exploratory). real-v3: 500 exported, ~0 labels | status record F0.2 |
| FT coverage figure | report built #103; **no measurement** (needs FT credentials + 7 days) | |
| Reproducibility | frozen clock, hashes, self-check baseline | `python -m src.evaluation self-check` |

Release relevance: the §P.5 "Ranking" criterion is *measured with CIs*; it holds with an explicit
"insufficient evidence" (4 relevant held-out labels). No promoted change exists, so "no change
worsens it" holds trivially.

### P3 Configuration — **partial; the main release gap**

| Item | State | Evidence |
|---|---|---|
| P3.1 Profile v2 | **partial**: example profile is `profile_version: 2`; the Profile page edits v2 target levels, education entries (`EducationEditor.tsx`) and `geography.home` (`HomePicker`), but still edits v1 fields (`experience.preferred_levels`, `excluded_seniority`, `max_years`, `max_education`) and has no editor for `experiences` facts; the example profile has no `home`, so distance scoring is off until the user sets one | `frontend/src/pages/Profile.tsx:1084–1300`, `config/profile.yaml` |
| P3.2 AI setup UI | **not implemented**: `/api/ai/runtimes`, `/hardware`, `/catalogue`, Ollama install, model pull, benchmark exist (#50, #61, #62); no frontend calls `/api/ai` | `src/api/routes/ai.py`; `grep -r "/api/ai" frontend/src` → nothing |
| AI enable switch | a "use AI" checkbox on the Dashboard run card (`Dashboard.tsx:504`) and `Phase2Config.ai_enabled` (env var); nothing checks that a runtime/model is set up first | `src/ai/config.py:35` |
| P3.3 Embedding first-run download UI | **not implemented**: the ONNX encoder downloads from the Hugging Face Hub on first use (`src/embeddings/onnx_encoder.py:96`) with no progress, size or retry UI; semantic matching fails open (#46) | |
| P3.4 Wizard | **not implemented**: `GettingStarted.tsx` is a static guide | |
| P3.5 Settings › Language, Data | language picker in the sidebar (`Sidebar.tsx:173`); Data (cleanup, erase, delete-all) done (#89, #124, #126, #128) | partial |
| P3.6 CV import | not implemented (tier T) | |

### P4 Ranking — **replaced by L1–L4 (ADR 0027); deterministic baseline stable**

Deterministic baseline authoritative and unchanged by learning (ADR 0002, `ranking_shadow` only
journals). Safety: synthetic self-check in CI; 0 new unsafe rejections required for any change
(R-C1). F7.3 posting-language flag: **not implemented** (`scoring.py` only penalises required
languages). No release blocker here: nothing is promoted, so there is no false-rejection regression
risk from ranking work.

### P5 AI assistance — **not implemented as a user feature**

Optional Phase-2 enrichment (`src/ai/pipeline.py`) and the LLM eligibility gate
(`src/ai/eligibility.py`) exist, off by default, local-only (`tests/test_llm_endpoint_local_only.py`).
F8.4 offer analysis (quotes, fit map, action plan), F8.5–F8.7: not implemented. The Offer page
shows no AI output. AI never decides eligibility on its own (ADR 0001).

### P6 User experience — **partial**

| Item | State |
|---|---|
| Design system, light/dark | done #48 |
| Pages | Dashboard, Jobs (lanes/Découverte shown in Jobs), JobDetail (explanation, provenance, application status, Save/Hide), Review, Quality, Lab, Profile, Settings, plus separate Pipeline/Stats/Skills (not yet merged into Insights) |
| Home sections (Strong / Worth a look / Needs your judgement, ADR 0026) | not built on Home |
| Applications tracker | status per offer only (`JobDetail.tsx:185`); no tracker page |
| i18n | EN/FR 1,246 lines each, pseudo-locale exists (`frontend/src/i18n/pseudo.ts`); backend prose → codes not finished (40 `detail=` strings in routes, not all user-facing) |
| Accessibility | no axe-core in the frontend tests; no keyboard-journey test |
| F9.2 disagreement browser, F9.3 strategy comparison, ES/IT/DE | not built (T) |

### P7 Release — **not started for 2.0**

| Item | State |
|---|---|
| Installer | v1 installer (Inno Setup `packaging/windows/FindYourJob.iss`, `build.ps1`) and its acceptance script (`acceptance.ps1`: install, launch, upgrade, uninstall) exist and passed for 1.0.0; F11.1 slim installer + components manifest not built |
| DB migrations | registry + backup before migrating (#85, `src/storage/migrations.py`), rehearsed on a 3.5 GB copy (P1); profile v1→v2 migration tested (`tests/test_profile_v2_migration.py`) |
| v1.0.0 → 2.0 upgrade on a built installer | not run |
| ADR 0027 learning gates | fault-injection pause, socket test, reset, erasure verification, lane tests exist; multi-occupation self-check impossible without L6 |
| Release test matrix, §H report | not run / not written |
| Usability (3 users), hardware (3 machines), remote labels | not done (owner) |
| Docs | README, `docs/windows-installer.md`, `docs/windows-app.md` describe 1.x; need 2.0 updates (wizard, tray, autostart, AI) |

---

## 5. Exact list of missing work

Numbered for reference in §6–§8.

1. W1 Status record update and L3 SHADOW decision recorded; commit the two L3 research reports (docs).
2. W2 Profile page fully on v2 (remove v1 editors, `experiences` editor, home prompt).
3. W3 Embedding model download UI (size, progress, SHA-256/revision check, retry, offline error).
4. W4 Autostart (start at sign-in) switch in Settings, wrapping `src/launcher/autostart.py`.
5. W5 France Travail credentials entry in Settings › Sources (stored locally, never logged), replacing the `.env` instruction.
6. W6 Onboarding wizard reusing the Profile, Sources, Background, embedding (and AI, if kept) components.
7. W7 AI for 2.0: either the AI setup UI (P3.2) with the Dashboard switch gated on it, or hide the switch (decision D3).
8. W8 EN/FR completeness: 0 missing keys, pseudo-locale clean, user-visible backend prose as codes.
9. W9 Accessibility: axe-core in frontend tests, keyboard-only onboarding and apply journey, AA contrast.
10. W10 Home sections per ADR 0026 (on deterministic tiers and existing lanes).
11. W11 2.0 installer (F11.1 slim + components manifest) and acceptance script updated for 2.0.
12. W12 Release test matrix on the built installer: fresh install, v1.0.0 upgrade, 24 h window closed, reboot, quit, offline/disk-full/port-taken, consent, blocked source.
13. W13 §H release report: every §P.5 criterion met or explicitly not met.
14. W14 Owner checks: usability (3 users), hardware (2–3 machines), 100 remote-type labels.
15. W15 Docs for 2.0 (README, installer and app guides).
16. W16 `matches_salary` correction + profile salary contract (plan first).
17. W17 FT coverage 7-day figure (needs W5 or owner credentials).
18. W18 P1 follow-ups (per-scope expiry, legacy rows, orphan rows, catch-up spacing, raw env-var message, two flaky tests).
19. W19 Insights merge (Pipeline/Stats/Skills), Applications tracker page, F9.2, F9.3, F7.3, ES/IT/DE.
20. W20 P5 AI analysis (F8.4) and F8.5–F8.7.
21. W21 L6 (after ROME licence + appellation checks), L7, L8 full, L9.
22. W22 L5 decisive real-v3 run (after the owner's real-v3 labelling).
23. W23 Worktree inventory and cleanup (R-Q9), private repo archive (owner).

---

## 6. Release blockers versus improvements

### A. Release blockers

| # | Why it blocks | Criterion (completion plan §P.5 / rules) |
|---|---|---|
| W1 | the record must match reality before release decisions; research evidence exists only on one disk | P.1 rule 6 |
| W2, W6 | a non-technical user cannot set up the app without the wizard and a coherent profile page | No-terminal, R-C5, P3 acceptance |
| W3 | owner decision R-Q4 | Embeddings |
| W4 | background collection with the window closed needs a terminal command today | Background, No-terminal |
| W5 | the first-run guide asks for environment variables | No-terminal (FT web keeps the app usable meanwhile; could move to B with decision D4) |
| W7 | an AI switch with no setup path is a broken user flow either way | AI optional (R-C3) |
| W8, W9 | release criteria | i18n, Accessibility |
| W11, W12, W13, W15 | release qualification | Quality gates, Migration, Startup and size, R-C4 |
| W14 | owner made usability release-blocking (R-A3); hardware 2 of 3 acceptable; remote labels "not met" until done (R-Q6) | Usability, Hardware, Remote |

### B. Important improvements (not blocking a first proper release)

W10 Home sections (required by the P6 acceptance but the Jobs page already carries tiers and
lanes; keep in B unless the owner wants it in 2.0), W16 `matches_salary` (latent: not reachable
from the UI), W17 FT coverage figure, W18 P1 follow-ups, W19 Insights/tracker/F9.2/F9.3/F7.3.

### C. Future research and optional features

W20 P5 AI analysis and documents, W21 L6–L9, W22 L5 real-v3 run, L3 activation research and the
ADR 0019 revision, ES/IT/DE, F6.7–F6.9, F8.8–F8.9, F11.2 update check.

---

## 7. Dependency analysis

```text
W1 records ─────────────────────────────────────────────┐
W2 Profile v2 ──┐                                        │
W3 embeddings ──┤                                        │
W4 autostart ───┼──► W6 wizard ──┐                       │
W5 FT creds ────┤                ├──► W11 installer ──► W12 matrix ──► W13 §H report ──► release (owner)
W7 AI decision ─┘                │        ▲
W8 i18n ◄── after W2–W7 strings ─┤        │
W9 a11y ◄── after W6 ────────────┘        │
W15 docs ◄── after W6, W11 ───────────────┘
W14 owner checks ◄── a 2.0 build (after W6; usability needs W8/W9)
W16 matches_salary ──► W21 L7          (independent of everything above)
ROME checks ──► W21 L6 ──► L7, L8 full, multi-occupation gate (D2)
real-v3 labels (owner) ──► W22 L5 run
```

Parallelizable: W2/W3/W4/W5 are independent of each other (different files: Profile page,
embeddings + Settings card, launcher + Settings card, Sources card). W16 and the ROME checks are
independent of the whole release path. The owner's labelling and the hardware probe can start as
soon as a 2.0 build exists. The owner prefers sequential work (one item in flight); the order
below is sequential, and the parallel options are listed only so the owner can choose.

---

## 8. Revised implementation order (milestones)

Each milestone: objective · current state · missing work · dependencies · expected result · tests and
acceptance · required before release.

### M0 Records and preservation (docs only)
1. Record the true state and preserve the L3 evidence.
2. Status record drifted (§4 P0); research reports uncommitted.
3. W1: update `2026-10-23-roadmap-status.md` (L3/L4 SHADOW final for 2.0, L5 merged, outbox verified, Profile partial, this audit); commit the two 2026-11-06 reports; no ADR edited.
4. None.
5. A record a new session can resume from.
6. Docs PR reviewed; links resolve; strategy fingerprint unchanged (no `config/` edit).
7. **Yes.**

### M1 Setup without a terminal (P3 core)
1. A non-technical user installs, sets up and gets first results with no file edit and no command.
2. Profile page mixed v1/v2; embedding download silent; autostart CLI-only; FT credentials via `.env`; no wizard.
3. W2, W3, W4, W5, then W6 (each its own issue and PR; W2 and W5 change contracts → plan first).
4. W2–W5 before W6; W7 decision before W6's AI step.
5. Fresh install → wizard → first search with live progress, AI absent.
6. Unit/API tests per item; profile migration tests on 3 fixture profiles + a copy of the owner's profile (R-A2 confirmation first); embedding download tests offline/interrupted/corrupt; credentials never in logs or API responses (test); frontend tests EN/FR; live fresh-copy run.
7. **Yes.**

### M2 AI for 2.0 (decision D3, then one of two paths)
1. No broken AI flow in 2.0.
2. Backend for runtimes, catalogue, pull, benchmark exists; no UI; the Dashboard switch is ungated.
3. Path A: P3.2 AI setup components shared by Settings › AI and the wizard; gate the switch on a verified model. Path B: hide the switch and the enrichment option; keep backend; P3.2 + P5 move to 2.1.
4. M1 (wizard step).
5. A: AI setup in the UI with "continue without AI". B: no user-facing AI in 2.0, stated in docs.
6. A: runtime missing / not running / not enough memory / too slow tests, benchmark verdict in words, 0 rejections caused by AI. B: test that no UI path enables Phase 2; full journey without AI.
7. **Yes** (either path).

### M3 Minimum polish
1. Meet the i18n and accessibility criteria.
2. EN/FR keys balanced, pseudo-locale exists; no axe; some backend prose.
3. W8, W9; W10 only if the owner keeps Home sections in 2.0.
4. After M1/M2 (their strings and screens).
5. 0 missing keys, pseudo-locale clean, axe 0 serious, keyboard-only onboarding and apply journey.
6. i18n key test, pseudo-locale run, axe-core in Vitest, manual keyboard journey recorded.
7. **Yes** (W10: no, unless D5).

### M4 Release qualification (P7)
1. Prove 2.0 works as a real installed application and report every criterion.
2. v1 installer and acceptance script exist; nothing run for 2.0.
3. W11, W12, W15, then W13.
4. M1–M3.
5. A 2.0 installer, a passed matrix, a §H report with each §P.5 criterion met or not met.
6. `acceptance.ps1` extended (wizard skipped headless, embedding download, tray, autostart, quit leaves no process); v1.0.0 → 2.0 upgrade with data and profile kept (backup + review screen); 24 h window-closed run; reboot; error recovery; ADR 0027 gates green; full pytest + frontend + audits on the release build; startup and size measured.
7. **Yes**; release itself needs the owner's approval.

### M5 Owner checks (parallel to M3–M4)
1. The human-only criteria.
2. Not started.
3. W14 (usability 3 users, hardware 2–3 machines, 100 remote labels); optionally FT credentials for W17.
4. A 2.0 build (after M1; usability ideally after M3).
5. Each criterion met or explicitly not met in the §H report.
6. Usability: onboarding < 10 min, 3 relevant offers saved < 5 min unaided; hardware probe on each machine; remote extractor ≥ 90 % on 100 labels.
7. **Yes** per R-A3/R-Q6, unless the owner waives (D6).

### After release (2.1+)
B items (W10 if deferred, W16 → then L7 groundwork, W17, W18, W19), then C items: P5 AI analysis
(W20), the ROME checks then L6 → L7 → L8 (W21), the L5 real-v3 run when labels exist (W22), L3
activation research (§10), L9.

### Why this order
- It front-loads the only work that stands between the current engine and a usable product
  (setup), instead of L6–L8 intelligence no release criterion needs.
- M1 before M2–M4 because every later step (strings, a11y, installer, usability) tests M1's screens.
- M2 as an explicit decision avoids building a full AI setup UI if the owner prefers to ship
  without user-facing AI.
- M5 runs in parallel because it waits on people, not code; starting it as soon as a 2.0 build
  exists keeps it off the critical path.
- Critical path: M0 → W2/W3/W4/W5 → W6 → M2 → M3 → W11 → W12 → W13 → owner approval.

---

## 9. Release acceptance checklist (2.0, without L3 activation)

Every line is reported met or **not met** in the §H report.

- [ ] Safety: 0 unsafe rejections on the synthetic self-check (CI) and on the personal sets (freshness-only reported apart).
- [ ] Ranking: held-out real-v2 precision@Strong and recall@20 with CIs shown, "insufficient evidence" stated; no promoted change.
- [ ] Learning stays inert: `activation_enabled: false`; EXPERIMENTAL disabled; every `weak_evidence_mode` = `reject`; `test_release_flags_default_off` green.
- [ ] Learning gates (ADR 0027): fault-injection pause, INV-2 socket test, reset bit-identity, erasure verification, lane caps green; multi-occupation self-check per decision D2.
- [ ] Fresh install, no credentials, unofficial sources off → FT web offers present.
- [ ] Background: daily run with the window closed for 24 h; disable works; reboot resumes (autostart switch in the UI); Quit leaves no process.
- [ ] Consent: unofficial sources off by default, explained, recorded, revocable.
- [ ] Source robustness: a failing source never fails the run; a changed page raises a canary error.
- [ ] No terminal: setup, sources (incl. FT credentials), background, autostart, AI (or its absence), quality, data reachable in the UI.
- [ ] AI: journey works without AI; AI setup in the UI (path A) or no user-facing AI (path B).
- [ ] Embeddings: first-run download with progress, integrity check, retry, offline error; app usable meanwhile.
- [ ] Migration: v1.0.0 → 2.0 upgrade keeps data; profile migrated with backup and review screen; rehearsed on a large DB copy.
- [ ] Startup ≤ 3 s window / ≤ 10 s warm / ≤ 25 s cold; installer < 100 MB; installed < 400 MB (measured).
- [ ] i18n: EN + FR complete, 0 missing keys, pseudo-locale clean.
- [ ] Accessibility: axe-core 0 serious; keyboard-only onboarding and apply journey; AA contrast.
- [ ] Hardware probe on 2–3 machines; usability test with 3 users; 100 remote labels (or each explicitly not met / waived).
- [ ] Security: Host/Origin/loopback/traversal probes, instance token, no outbound request with profile data.
- [ ] Removals: no Discord/n8n/digest code or settings.
- [ ] All suites, audits and the installer acceptance green on the release build; docs updated.
- [ ] Owner approval to release and tag.

---

## 10. Deferred work

| Item | Why deferred | What is preserved | Re-entry condition |
|---|---|---|---|
| **L3 activation (personal ranking ACTIVE / EXPERIMENTAL)** | simulator criteria not met (false activation up to 21.6 %); 2026-11-06/07 research: passive evidence ~1–2 units/month, no valid design with useful power now | L3 code and shadow journal, `ranking_shadow`, health record, simulator, `docs/reports/2026-11-05-l3-simulator.md`, the two 2026-11-06 reports (to commit in M0), `activation_enabled: false` | enough real AD-6 volume, and a separately approved design (e.g. randomized evidence), its own ADR |
| ADR 0019 revision (sequential test) | only serves activation | research reports | with the item above |
| L5 decisive run | needs real-v3 labels | pre-registration `docs/lab/preregistered/2026-11-05-ad2-weak-evidence.json`, inert code | owner says real-v3 labelling is finished |
| L6 occupations | not needed for release | ADR 0022, 0023 | ROME licence + appellation checks done |
| L7 market | needs L6, `matches_salary`, salary contract | ADR 0024 | L6 + W16 |
| L8 full self-monitoring | needs L6 | ADR 0025, existing detectors | L6 |
| L9 | optional | AD-3.2 | offline validation on logged data |
| P5 AI analysis, documents, translation | not release-necessary if D3 = path B | backend AI stack | after 2.0 |
| ES/IT/DE, F6.7–F6.9, F8.8–F8.9, F11.2 | optional tiers | plan text | after 2.0 |

---

## 11. Decisions for the owner

| # | Decision | Recommendation | Why it needs you |
|---|---|---|---|
| D1 | Run P3 → minimum P6 → P7 **before** L6–L8 (L6–L8 after 2.0) | yes | reverses the order approved in ADR 0027; needs a new ADR or a recorded owner decision (ADRs are not edited by this audit) |
| D2 | Multi-occupation self-check gate when L6 is not in 2.0 | replace it for 2.0 by "no occupation logic in production", restore it with L6 | an ADR 0027 release gate |
| D3 | AI in 2.0: path A (AI setup UI) or path B (no user-facing AI, P3.2 + P5 in 2.1) | B for the shortest path; A if AI is part of the 2.0 promise | product scope; R-C3 |
| D4 | France Travail credentials entry: release blocker or improvement | blocker (cheap, removes the only `.env` instruction) | R-C5 interpretation |
| D5 | Home sections (ADR 0026) in 2.0 | defer to 2.1 | P6 acceptance scope |
| D6 | Owner-only criteria (usability, hardware, remote labels): required or explicitly waived for 2.0 | keep usability (R-A3) and hardware (2 machines); allow remote labels "not met" | your earlier decisions R-A3, R-Q6 |
| D7 | Commit the two 2026-11-06 L3 research reports | yes, as a docs PR in M0 | they are your evidence; not yet reviewed for publication |
| D8 | Confirm the everyday database (R-A2) | needed before the M1 profile-migration rehearsal on your real profile | still open since 2026-10-30 |

---

## References

- Plans: `docs/plans/2026-10-23-final-update-plan.md`, `2026-10-30-completion-plan.md` (§P.2, §P.5),
  `2026-10-23-roadmap-status.md`, `2026-11-05-learning-architecture-decisions.md` (§1.1, §5, §6, §8).
- ADRs: 0001, 0002, 0014–0027 (`docs/adr/`).
- Reports: `docs/reports/2026-11-05-l3-simulator.md`, `2026-11-05-l5-weak-evidence-exploratory.md`.
- Code: `src/pipeline/filter.py:562` (`matches_salary`), `:859` (`RANKABLE_CHECKS`),
  `config/personalization.yaml:83` (`activation_enabled`), `config/filters.yaml` (`weak_evidence_mode`),
  `src/api/routes/ai.py`, `src/ai/config.py:35`, `src/embeddings/onnx_encoder.py:96`,
  `src/launcher/autostart.py`, `frontend/src/components/GettingStarted.tsx:11`,
  `frontend/src/pages/Profile.tsx`, `frontend/src/pages/Dashboard.tsx:504`,
  `src/storage/migrations.py`, `packaging/windows/acceptance.ps1`.
- Tests: `tests/test_learning_health.py`, `test_privacy_erasure.py`, `test_privacy_learning.py`,
  `test_personal_data_stays_local.py`, `test_weak_evidence_lanes.py`, `test_evaluation_isolation.py`,
  `test_lab_guard.py`, `test_storage_migrations.py`, `test_profile_v2_migration.py`.
- PRs (public): #71–#75 (P0), #77–#93 (P1), #94–#109 (P2), #110–#135 (L0, L1), #132–#136 (L2),
  #137–#140, #143 (L3), #141, #142, #144, #145 (L4), #146–#150 (L5), #151–#155 (fixes).
