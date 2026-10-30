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
| F0.2 personal labels | T | owner data exists | `real-v1` 250 labels (K2), not yet read by the self-check; blind real-v2 labelling in the app = completion plan P2.3 |
| F0.3 FT coverage benchmark | T | ready | needs 7 calendar days of background runs (P1) |
| F1.1–F1.3 removals | R | merged #40, #58, #74; **no UI** for F1.3 | source health API has no screen yet (P1.2); leftover wording cleaned in #74 (the 5678 probe stays) |
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
| F6.3 Indeed via jobspy | R | merged #68 | opt-in only through `data/settings.json` until F6.5 (P1.2) |
| F6.4 background pacing | R | merged #70 | a 429 stops the scan, cooldown 1 h → 24 h, resumes slower; opted-in sources daily; consent screen = F6.5 (P1.2) |
| F6.5 consent | R | ready | P1.2 |
| F6.6 expiry + provenance | R | ready | P1.4 (contract: own plan; migration registry P1.3 first) |
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
| F10.3 settings | T → **R** | ready | promoted by R-Q3/R-C2/R-C3: Settings › Sources (P1.2), AI (P3.2), Data/Language (P3.5) |
| F11.1 slim installer | R | blocked | P7 |
| New: daily pipeline schedule | R | ready | audit D1, R-Q3 → P1.1 |
| New: embedding first-run download UI | R | ready | audit D8, R-Q4 → P3.3 |
| New: port evaluation work from the private repo | R | ready | R-Q7 → P2.1 |

## Next actions

Owner instructions: one item at a time, in order; finish, review and merge before starting the
next (2026-10-25). Order from the completion plan §P (approved 2026-10-30):

1. P0 done (2026-10-30): #71 record, #70 merged, #72 CI fix, #73 version `2.0.0.dev0`, #74
   leftovers, local worktree inventory and cleanup (57 merged worktrees removed, the rest kept with
   reasons; inventory kept outside the repo with the owner's data).
2. P1 (next): daily pipeline schedule → Settings › Sources and background → migration registry →
   expiry + provenance → source follow-ups.
3. P2: ports (ranking metrics, sampling cohorts, top-scored stratum, rejected-pool scan, approval
   diff) → pre-registration → in-app blind calibration (owner labels 100 representative offers) →
   held-out split → personal sets in the self-check and Quality page → FP analysis → FT coverage.
4. P3 (while labels are collected): profile v2 UI, AI setup UI, embedding download, wizard,
   Settings › Data/Language, CV import.
5. P4 ranking quality, P5 AI analysis, P6 polish, P7 release qualification (owner approval to release).

Blocked on the owner: confirm which database the everyday app uses (before P1.4/P2.3); 100 blind
labels (P2.3); usability testers and machines (P7).

Follow-ups recorded from reviews: CORS allows the Vite dev origin when `API_SAME_USER_ONLY` is off (pre-existing); installer pins have no hashes (`--require-hashes`); retire `scripts/install_startup.py`; ~~drop 5678 from the lab relay ports~~ (won't do: an n8n left by a pre-2.0 install can still relay to the API, so the probe stays, #74); EURES robots `Crawl-delay: 10` not honoured; backend `apply_url` scheme validation.

## Log

- 2026-10-23: execution kickoff; owner decisions K1–K4 recorded; #44 CI failure (undeclared `beautifulsoup4`) fixed.
- 2026-10-23: wave 0 reviewed and merged: #39, #41, #40 (after fixes), #43, #42 (after fixes: autostart refused outside the installed app, failed start reported), #44 (after fixes: robots.txt fails closed). Main 46c4f0f.
- 2026-10-25: merged #45–#59: F0.1, F1.4, F2 rest, F3 fail-open, F4.3, F6.1, F7.1, F8.1, F9.1, F10.1. Every contract or security PR had a review pass; fixes landed before merge.
- 2026-10-25/26: merged #61 (F8.2), #62 (F8.3), #64 (F5.1), #65 (F5.2), #66 (F5.3), #68 (F6.3). Main 8b30e37. Draft #70 (F6.4) opened with the 2026-10-26 owner decisions.
- 2026-10-30: project audit on 8b30e37 (`pytest -m "not network"` 5268 passed, 9 skipped; synthetic self-check PASS, unchanged). Findings and the owner-approved execution order: `2026-10-30-completion-plan.md`.
- 2026-10-30: #70 (F6.4) self-reviewed and merged under K1 (owner decision R-Q2). Main 4eb1f03.
- 2026-10-30: `main` CI turned red after CI picked up pytest 9.2.0 (its `monkeypatch.setattr` deleted Starlette `app.state` attributes on teardown; 78 errors, not caused by #70). Fixed by #72 (tests patch `app.state._state` with `setitem`, regression test).
- 2026-10-30: #73 (`2.0.0.dev0`) and #74 (n8n/Discord wording; a comment edit in `config/scoring.yaml` was reverted because the strategy fingerprint hashes config bytes) merged. Phase 0 complete. Main 9031f0c.
