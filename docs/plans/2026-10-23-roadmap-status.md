# 2.0 roadmap: execution status

The living execution record for `2026-10-23-final-update-plan.md` (the plan stays the source of
truth for scope and acceptance criteria). Since 2026-10-30 the execution **order**, the audit's
corrections and the owner's review decisions are in `2026-10-30-completion-plan.md`. A new session
resumes from **Next actions**.

Tiers: **R** release-blocking, **T** release-targeted (own toggle), **O** optional. Status:
`done` (merged, criterion verified), `merged` (merged, criterion partly verified), `PR` (open),
`ready` (dependencies met), `blocked` (waiting on another item), `owner` (needs the owner).
`no UI` marks a merged backend whose user interface is still missing (a user-facing item is not
done until a user can use it from the app, completion plan rule R-C2).

## Owner decisions (2026-10-23, execution kickoff)

| # | Decision |
|---|---|
| K1 | The agent squash-merges a PR once CI is green and it has been self-reviewed (a code-review pass for contract or security PRs), with the evidence on the PR. Releases, tags and repository settings still need explicit owner approval. |
| K2 | Personal labels already exist: `data/benchmarks/real-v1` holds 250 labels by the owner (2026-10-09). They are the F0.2 personal evaluation set. Caveat: labelled with assist proposals visible, so not blind. `real-v2` (310 offers) is unlabelled. |
| K3 | The agent may start the installed Ollama / LM Studio and download 2–4 candidate models (up to ~20 GB) to choose the F8 model catalogue. |
| K4 | The window is **pywebview / WebView2**, as in D3, with an Edge `--app` window as the fallback when WebView2 is missing. #42's Edge-only window is an interim step. |

Owner decisions 2026-10-26 (#69/#70): opted-in unofficial sources scan **daily automatically**; the
cooldown after a 429 is **1 h, doubled per repeat, capped at 24 h**.

Owner review decisions 2026-10-30 (R-Q1–R-Q9, R-C1–R-C5, R-A1–R-A5): see
`2026-10-30-completion-plan.md` §R. In short: daily background collection on by default for
official/public sources (R-Q3); embedding model downloaded on first run with progress and SHA-256
(R-Q4); 100 representative real-v2 offers labelled blind in the app (R-Q5, R-A5); unmet criteria
stay unmet (R-Q6); the 3-user usability test is release-blocking, 2 of 3 machines acceptable for the
hardware check (R-A3); every user-facing feature ships with its UI (R-C2); measurement before
matching changes, judged on held-out labels (R-C1).

## Dependency waves (original plan)

| Wave | Items | Depends on |
|---|---|---|
| 0 | #39 plan, #40 F1.1–F1.3, #41 F4.1–F4.2, #42 F2 (tray), #43 F3.1, #44 F6.2 | — |
| 1 | F0.1 baselines · F1.4 removals · F2.1 rest (dynamic port, instance token) + F2.2 pywebview + F2.4/F2.5 · F6.1 source contract · F8.1 runtime detection + hardware probe · F10.1 design system + F7.1 i18n skeleton | wave 0 merged |
| 2 | F4.3 location scoring (profile home/radius/remote policy) · F6.3 Indeed via jobspy · F6.4 background pacing · F6.5 consent · F6.6 expiry + provenance · F8.2 catalogue + install + pull · F8.3 benchmark · F0.3 FT coverage (7 calendar days) | F4.1, F6.1, F8.1 |
| 3 | F5.1 profile v2 + migration · F5.2 career stages · F5.3 education levels · F8.4 offer analysis + queue · F9.1 Quality page · F7.3 posting language | F4.3, F8.3 |
| 4 | F5.4 wizard · F5.5 CV import · F8.5 documents · F8.6 discovery · F8.7 translation · F9.2 · F9.3 · F10.2 · F10.3 · F10.4 | F5.1, F8.4, F10.1, F7.1 |
| 5 | F7.2 translations · F11.1 slim installer + components manifest · full acceptance · §H report · 2.0 release (owner approval) | everything R |
| O | F6.7 ATS auto-follow · F6.8 La Bonne Alternance + public sector · F6.9 URL paste · F8.8 translation pack · F8.9 LLM gate in lab · F11.2 update check | scheduled after R/T, not dropped |

The phase order that supersedes the waves from 2026-10-30 is in the completion plan §P.

## Item status

| Item | Tier | Status | Where / evidence |
|---|---|---|---|
| F0.1 baselines | R | done #45 | `docs/baselines/v1.0.0.md`; full run duration not measured |
| F0.2 personal labels | T | flow merged #99, #104; labels pending | blind in-app labelling of real-v2 (Quality › Calibration) and held-out measurement with CIs are merged; real-v2 has 0 labels until the owner labels it |
| F0.3 FT coverage benchmark | T | report merged #103; measurement pending | Quality shows the share of API offers the website reader also found; needs FT API credentials and 7 days of both sources |
| F1.1–F1.3 removals | R | done #40, #58, #74, #83 | source health, last error and breaker reset in Settings › Sources (#83); leftover wording cleaned in #74 (the 5678 probe stays) |
| F1.4 APEC, WTTJ browser, crawl4ai | R | done #51 | old configs listing them are skipped with a reason |
| F2.1–F2.5 desktop | R | merged #42, #52, #54, #55 | installer acceptance with window, tray, autostart and quit not run yet (P7) |
| F3.1 ONNX | R | done #43, #46 | Spearman 1.000 on 1,000 jobs; installer 62.1 MB without the model; first-run download UI = P3.3 |
| F4.1–F4.2 geo | R | merged #41 | resolver 100 % on 483 FT; remote extractor: 100 owner labels **not met** (R-Q6) |
| F4.3 distance scoring | R | done #56, #57 | only active once a profile has a `home`; the shipped example has none (P3.1) |
| F5.1 profile v2 + migration | R | merged #64 | Profile page still edits v1 fields (P3.1) |
| F5.2 career stages | R | merged #65 | title-only conflicts go to review; keyword rejection off with targets |
| F5.3 education levels | R | merged #66 | French/EQF 3–8 |
| F5.4 wizard | R | ready | P3.4 |
| F5.5 CV import | T | ready | P3.6 |
| F6.1 source contract | R | done #58 | access classes, robots, rate caps, breaker + reset API, parser canary |
| F6.2 FT web | R | merged #44 | F0.3 coverage report pending |
| F6.3 Indeed via jobspy | R | merged #68, #83 | opt-in in Settings › Sources with its risk sentence and date (#83) |
| F6.4 background pacing | R | merged #70, #91 | a 429 stops the scan, cooldown 1 h → 24 h, resumes slower; opted-in sources daily; robots.txt `Crawl-delay` honoured (EURES 10 s, #91) |
| F6.5 consent | R | merged #83, #92 | off by default, risk sentence, choice stored with its date; a scan started by hand is never taken for a choice (#92; a 1.x install's scan history is recorded once as its choice) |
| F6.6 expiry + provenance | R | merged #85, #87, #88, #90, #89 | provenance "Seen on …"; expiry by end date, user report, or misses counted **only by sources that read their whole listing** (#90: FT web shows 20 of 961 offers per page); Settings › Data cleanup with a safety copy first (#89), rehearsed on a copy of the 3.5 GB DB |
| F7.1 i18n skeleton | R | merged #53 | EN+FR 780 keys; backend prose → codes still to do |
| F7.2 EN+FR (R), ES/IT/DE beta (T) | R/T | blocked | P6 |
| F7.3 posting language | T | ready | P4 |
| F8.1 runtime detection + hardware | R | merged #50, **no UI** | P3.2; probe on 1 machine of 3 |
| F8.2 catalogue + install + pull | R | merged #61, **no UI** | P3.2 |
| F8.3 verify + benchmark | R | merged #62, **no UI** | P3.2 |
| F8.4 offer analysis | R | blocked | P5 |
| F8.5–F8.7 | T | blocked | F8.4 |
| F9.1 Quality page | R | done #59 | promotion stays CLI (I3) |
| F9.2–F9.3 | T | ready | P6 |
| F10.1 design system | R | done #48 | |
| F10.2, F10.4 pages | R | ready | P6 (each phase ships its own screens, R-C2) |
| F10.3 settings | T → **R** | partly merged | Background, Daily runs, Sources (#81, #83, #92) and Data (#89) done; AI (P3.2), Language (P3.5) to do |
| F11.1 slim installer | R | blocked | P7 |
| New: daily pipeline schedule | R | merged #77, #79, #81, #83, #92 | `pipeline_daily` once per local day at the chosen time after the profile is saved, one catch-up run, waits for scans, a manual run counts; a run that reached no source is retried (#92); background switch and Daily runs card in Settings |
| New: embedding first-run download UI | R | ready | audit D8, R-Q4 → P3.3 |
| New: port evaluation work from the private repo | R | merged #94–#98, #101 | private #130, #133, #131, #132, #129 ported and #134 kept as history; the private PRs can now be closed with links and the private repo archived (owner action) |

## Next actions

Owner instructions: one item at a time, in order; finish, review and merge before starting the
next (2026-10-25). Order from the completion plan §P (approved 2026-10-30):

1. P0 done (2026-10-30): #71 record, #70 merged, #72 CI fix, #73 version `2.0.0.dev0`, #74
   leftovers, local worktree inventory and cleanup (57 merged worktrees removed, the rest kept with
   reasons; inventory kept outside the repo with the owner's data).
2. P1 merged (2026-10-30): daily schedule (#77, #79, #81), Settings › Sources and background
   (#83), migration registry (#85), provenance + expiry (#87, #88, fixed by #90), Settings › Data
   (#89), D12 source/security follow-ups (#91), scheduler/Settings review fixes (#92). Acceptance
   still **not verified as stated**: the 24 h window-closed run from a fresh install and the
   FT web + EURES duplicate with two provenances in a real run (tests cover both mechanisms; see the
   completion plan, P1 status).
3. P2 merged (2026-10-30): ports #94–#98, pre-registration #101, calibration #99, held-out split #100,
   ranking CIs #102, personal measurement and FP analysis #104, FT coverage #103. Not met: the owner's
   100 blind labels, the v2-profile baseline (P3.1), the 7-day FT coverage figure.
4. P3 (while labels are collected): profile v2 UI, AI setup UI, embedding download, wizard,
   Settings › Data/Language, CV import.
5. P4 ranking quality, P5 AI analysis, P6 polish, P7 release qualification (owner approval to release).

Blocked on the owner: 100 blind labels in Quality › Calibration (from a 2.0 build: v1.0.0 has no such
page); France Travail API credentials for the 7-day coverage figure; confirm the everyday database before
any migration runs on it. The Desktop and Start-menu shortcuts open the v1.0.0 install, so it is most
likely `%LOCALAPPDATA%\Programs\Find Your Job With AI V1\data\jobs.db` (27 MB, last written
2026-10-23); real-v1/real-v2 come from the private `data/jobs.db` (3.5 GB). Usability testers and machines (P7).

Follow-ups recorded from reviews: the D12 items (dev origins, installer hashes, `install_startup.py`, EURES `Crawl-delay`, link schemes) are closed by #91; the 5678 lab relay probe stays (#74). Open, proposed as issues: per-scope expiry completeness (an offer from a query or board the user removed is still counted missing by a complete source); 463 legacy `status = 'inactive'` rows in the owner's large DB; pre-existing orphan rows in derived tables (3,624 embeddings, 3,722 personalization cache rows, 158 FTS, 60 AI analyses on the owner's DB copy); a startup catch-up run can be followed within the hour by the new period's run.

## Log

- 2026-10-23: execution kickoff; owner decisions K1–K4 recorded; #44 CI failure (undeclared `beautifulsoup4`) fixed.
- 2026-10-23: wave 0 reviewed and merged: #39, #41, #40 (after fixes), #43, #42 (after fixes: autostart refused outside the installed app, failed start reported), #44 (after fixes: robots.txt fails closed). Main 46c4f0f.
- 2026-10-25: merged #45–#59: F0.1, F1.4, F2 rest, F3 fail-open, F4.3, F6.1, F7.1, F8.1, F9.1, F10.1. Every contract or security PR had a review pass; fixes landed before merge.
- 2026-10-25/26: merged #61 (F8.2), #62 (F8.3), #64 (F5.1), #65 (F5.2), #66 (F5.3), #68 (F6.3). Main 8b30e37. Draft #70 (F6.4) opened with the 2026-10-26 owner decisions.
- 2026-10-30: project audit on 8b30e37 (`pytest -m "not network"` 5268 passed, 9 skipped; synthetic self-check PASS, unchanged). Findings and the owner-approved execution order: `2026-10-30-completion-plan.md`.
- 2026-10-30: #70 (F6.4) self-reviewed and merged under K1 (owner decision R-Q2). Main 4eb1f03.
- 2026-10-30: `main` CI turned red after CI picked up pytest 9.2.0 (its `monkeypatch.setattr` deleted Starlette `app.state` attributes on teardown; 78 errors, not caused by #70). Fixed by #72 (tests patch `app.state._state` with `setitem`, regression test).
- 2026-10-30: #73 (`2.0.0.dev0`) and #74 (n8n/Discord wording; a comment edit in `config/scoring.yaml` was reverted because the strategy fingerprint hashes config bytes) merged. Phase 0 complete. Main 9031f0c.
- 2026-10-30: P1 merged: #77, #79, #81 (daily pipeline at a local time, manual run counts, background switch), #83 (Settings › Sources and background, consent), #85 (migration registry), #87 (provenance), #88 (expiry), #90 (expiry counts a miss only after a **complete** collection: a France Travail web page lists 20 of 961 offers, so #88 alone would have expired most open FT web offers in 3 days), #89 (Settings › Data cleanup: safety copy first, no search or scan during it; rehearsed on a copy of the 3.5 GB DB: 14,704 offers removed in 71 s, 3.68 → 3.25 GB, integrity ok, protected offers kept), #91 (D12: dev origins in dev mode only, http(s) links only, `Crawl-delay`, `install_startup.py` retired, installer `--require-hashes` verified on CPython 3.12), #92 (a manual scan is never a source choice; a daily run that reached no source is retried; background-off wording).
- 2026-10-30: live check of a fresh copy of `main` 371cd6d (own empty `data/`, example profile): before the profile was saved nothing ran and LinkedIn/HelloWork showed "not chosen"; once it was saved the scheduler started `pipeline_daily` by itself (trigger `scheduled`), which completed in 8.4 min (FT web 640 collected, EURES 1,824 with its 10 s Crawl-delay, RemoteOK 401, WWR 195, Jobicy 75; 691 offers stored; FT API and Indeed skipped with reasons; no source claimed a complete listing, so no miss was counted). Settings showed the run, its time, the next run (next day 08:00) and each source's result. No cross-source duplicate occurred in that run. Not done: the 24 h window-closed run. UX gap noted: the FT API row shows a raw environment-variable message and "Collected" while skipped.
- 2026-10-30: final P1 checks on `main` 371cd6d: `pytest -m "not network"` 5396 passed, 9 skipped, 0 failed; frontend lint clean, 294 tests passed, build OK. Phase 1 merged; next: Phase 2.
- 2026-10-30: P2 merged: #94–#104 (see the completion plan, P2 status). Final check on the P2 head (all of #94–#104): `pytest -m "not network"` 5456 passed, 9 skipped, 0 failed; frontend 301 tests passed, build OK. One timing-flaky test seen once (`test_scheduler_service.py::TestAutoPipelineTrigger::test_successful_scan_triggers_pipeline`, passes 3/3 alone), proposed as an issue.
