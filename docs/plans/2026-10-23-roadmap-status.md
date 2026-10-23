# 2.0 roadmap: execution status

The living execution record for `2026-10-23-final-update-plan.md` (the plan stays the source of
truth for scope and acceptance criteria). A new session resumes from **Next actions**.

Tiers: **R** release-blocking, **T** release-targeted (own toggle), **O** optional. Status:
`done` (merged, criterion verified), `merged` (merged, criterion partly verified), `PR` (open),
`ready` (dependencies met), `blocked` (waiting on another item), `owner` (needs the owner).

## Owner decisions (2026-10-23, execution kickoff)

| # | Decision |
|---|---|
| K1 | The agent squash-merges a PR once CI is green and it has been self-reviewed (a code-review pass for contract or security PRs), with the evidence on the PR. Releases, tags and repository settings still need explicit owner approval. |
| K2 | Personal labels already exist: `data/benchmarks/real-v1` holds 250 labels by the owner (2026-10-09). They are the F0.2 personal evaluation set. Caveat: labelled with assist proposals visible, so not blind. `real-v2` (310 offers) is unlabelled. |
| K3 | The agent may start the installed Ollama / LM Studio and download 2–4 candidate models (up to ~20 GB) to choose the F8 model catalogue. |
| K4 | The window is **pywebview / WebView2**, as in D3, with an Edge `--app` window as the fallback when WebView2 is missing. #42's Edge-only window is an interim step. |

Not yet decided (asked when they block): the usability test with 3 non-technical users (F5.4, §H
UX), the hardware probe on 3 machines (F8.1), and 100 owner-labelled remote postings (F4.2).
Until then, they are reported as *not met* rather than replaced by agent evidence.

## Dependency waves

| Wave | Items | Depends on |
|---|---|---|
| 0 | #39 plan, #40 F1.1–F1.3, #41 F4.1–F4.2, #42 F2 (tray), #43 F3.1, #44 F6.2 | — |
| 1 | F0.1 baselines · F1.4 removals · F2.1 rest (dynamic port, instance token) + F2.2 pywebview + F2.4/F2.5 · F6.1 source contract · F8.1 runtime detection + hardware probe · F10.1 design system + F7.1 i18n skeleton | wave 0 merged |
| 2 | F4.3 location scoring (profile home/radius/remote policy) · F6.3 Indeed via jobspy · F6.4 background pacing · F6.5 consent · F6.6 expiry + provenance · F8.2 catalogue + install + pull · F8.3 benchmark · F0.3 FT coverage (7 calendar days) | F4.1, F6.1, F8.1 |
| 3 | F5.1 profile v2 + migration · F5.2 career stages · F5.3 education levels · F8.4 offer analysis + queue · F9.1 Quality page · F7.3 posting language | F4.3, F8.3 |
| 4 | F5.4 wizard · F5.5 CV import · F8.5 documents · F8.6 discovery · F8.7 translation · F9.2 · F9.3 · F10.2 · F10.3 · F10.4 | F5.1, F8.4, F10.1, F7.1 |
| 5 | F7.2 translations · F11.1 slim installer + components manifest · full acceptance · §H report · 2.0 release (owner approval) | everything R |
| O | F6.7 ATS auto-follow · F6.8 La Bonne Alternance + public sector · F6.9 URL paste · F8.8 translation pack · F8.9 LLM gate in lab · F11.2 update check | scheduled after R/T, not dropped |

## Item status

| Item | Tier | Status | Where / evidence |
|---|---|---|---|
| F0.1 baselines | R | PR #45 | `docs/baselines/v1.0.0.md`; full run duration not measured |
| F0.2 personal labels | T | owner data exists | `real-v1` 250 labels (K2); the self-check must report on them |
| F0.3 FT coverage benchmark | T | ready | #44 merged; needs 7 calendar days of runs |
| F1.1–F1.3 removals | R | merged #40 | review fixes 6d951ab; F1.3 "Settings shows source health" still open (`source_health.py` has no consumer yet) |
| F1.4 APEC, WTTJ browser, crawl4ai | R | in progress | worktree fyj-pub-rm2 |
| F2.1–F2.5 desktop | R | merged #42 (tray, autostart, stop, restart); rest in progress | worktree fyj-pub-win: dynamic port + instance token, pywebview window (K4), notification categories, kill-and-resume |
| F3.1 ONNX | R | done #43 | Spearman 1.000 on 1,000 jobs; installer 62.1 MB; fail-open follow-up PR #46 |
| F4.1–F4.2 geo | R | merged #41 | resolver 100 % on 483 FT; remote extractor: agent review, owner labels pending |
| F4.3 distance scoring | R | in progress | worktree fyj-pub-loc (2 PRs: matching, baseline re-record) |
| F5.1–F5.4 profile v2, onboarding | R | blocked | F4.3 |
| F5.5 CV import | T | blocked | F5.1 |
| F6.1 source contract | R | ready | |
| F6.2 FT web | R | merged #44 | robots fail-closed + 5 review fixes (aea09d1); F0.3 coverage report pending |
| F6.3–F6.6 | R | blocked | F6.1 (F6.6 also F4.1) |
| F7.1 i18n skeleton | R | in progress | worktree fyj-pub-ds (stacked on F10.1) |
| F7.2 EN+FR (R), ES/IT/DE beta (T) | R/T | blocked | pages rebuilt |
| F7.3 posting language | T | ready | |
| F8.1–F8.4 AI platform + analysis | R | F8.1 in progress | worktree fyj-pub-ai1; K3 |
| F8.5–F8.7 | T | blocked | F8.4 |
| F9.1 Quality page | R | blocked | F10.1 |
| F9.2–F9.3 | T | blocked | F9.1 |
| F10.1 design system | R | in progress | worktree fyj-pub-ds |
| F10.2, F10.4 pages | R | blocked | F10.1, F7.1 |
| F10.3 settings/insights/quality | T | blocked | F10.1 |
| F11.1 slim installer | R | blocked | wave 4 |

## Next actions

1. Review and merge the in-flight PRs as they turn green: #45, #46, F8.1, F10.1/F7.1, F2 rest, F1.4, F4.3.
2. Then F6.1 source contract (after F1.4), F5.1 profile v2 (after F4.3), F8.2/F8.3 model catalogue and benchmark (after F8.1), F0.3 coverage runs.

## Log

- 2026-10-23: execution kickoff; owner decisions K1–K4 recorded; #44 CI failure (undeclared `beautifulsoup4`) fixed.
- 2026-10-23: wave 0 reviewed and merged: #39, #41, #40 (after fixes), #43, #42 (after fixes: autostart refused outside the installed app, failed start reported), #44 (after fixes: robots.txt fails closed). Main 46c4f0f.
