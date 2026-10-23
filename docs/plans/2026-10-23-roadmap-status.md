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
| F0.1 baselines | R | ready | v1 figures measured in the plan (§A2) and in #43; to commit under `docs/baselines/` |
| F0.2 personal labels | T | owner data exists | `real-v1` 250 labels (K2); the self-check must report on them |
| F0.3 FT coverage benchmark | T | blocked | needs #44 merged, then 7 days of runs |
| F1.1–F1.3 removals | R | PR #40 | CI green |
| F1.4 APEC, WTTJ browser, crawl4ai | R | ready | tripwire test to update |
| F2.1–F2.5 desktop | R | PR #42 (tray, autostart, stop, restart) | remaining: dynamic port + instance token, pywebview window (K4), toast categories, kill-and-resume test |
| F3.1 ONNX | R | PR #43 | Spearman 1.000 on 1,000 jobs; installer 62.1 MB |
| F4.1–F4.2 geo | R | PR #41 | resolver 100 % on 483 FT; remote extractor: agent review, owner labels pending |
| F4.3 distance scoring | R | blocked | #41 |
| F5.1–F5.4 profile v2, onboarding | R | blocked | F4.3 |
| F5.5 CV import | T | blocked | F5.1 |
| F6.1 source contract | R | ready | |
| F6.2 FT web | R | PR #44 | bs4 declared (CI fix 701963c) |
| F6.3–F6.6 | R | blocked | F6.1 (F6.6 also F4.1) |
| F7.1 i18n skeleton | R | ready | with F10.1 |
| F7.2 EN+FR (R), ES/IT/DE beta (T) | R/T | blocked | pages rebuilt |
| F7.3 posting language | T | ready | |
| F8.1–F8.4 AI platform + analysis | R | ready (F8.1) | K3 |
| F8.5–F8.7 | T | blocked | F8.4 |
| F9.1 Quality page | R | blocked | F10.1 |
| F9.2–F9.3 | T | blocked | F9.1 |
| F10.1 design system | R | ready | |
| F10.2, F10.4 pages | R | blocked | F10.1, F7.1 |
| F10.3 settings/insights/quality | T | blocked | F10.1 |
| F11.1 slim installer | R | blocked | wave 4 |

## Next actions

1. Merge wave 0 in order #39 → #40 → #41 → #43 → #42 → #44, re-running CI after each conflict fix.
2. Start wave 1 in parallel worktrees (one branch and PR per item).

## Log

- 2026-10-23: execution kickoff; owner decisions K1–K4 recorded; #44 CI failure (undeclared `beautifulsoup4`) fixed.
