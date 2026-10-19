# V1 status record

The working record of where V1 stands. It states only what was verified, with the evidence;
anything not re-run says so. Last update: 2026-10-19 (round 4: integration, Windows installer,
user walkthrough).

**Verdict: V1 READY WITH EXPLICIT LIMITATIONS.** Every change is merged into `main`. The
Windows installer was built from `main` and passed an automated install → launch → stop →
upgrade → uninstall test (60 checks) and a user walkthrough on real offers. The limitations
are listed under [Residual risks](#residual-risks). The main ones:

- Real-world accuracy and ranking quality are not measured: no human-reviewed real labels,
  no graded relevance labels.
- The lab agent's OS-level isolation (a separate Windows account) is not set up; under the
  user's own account only the in-process guard and human approval protect production.
- The installer is not code-signed (SmartScreen warning).
- APEC refuses automated access (bot protection with a CAPTCHA); runs show it as failed.

Status words: **verified** (shown by tests or by running it), **implemented, not verified**,
**partial**, **blocked**, **not implemented**.

## Delivery state

All work is merged into `main` by squash. Required checks (`test`, `frontend`, `gitleaks`)
and the advisory ones (`ruff`, `python-audit`) were green on each merged head.

| PR | Change |
|---|---|
| #1 | required `frontend` check runs on every PR |
| #2 | a wished-for education level is not a ceiling; CI unsafe-rejection gate |
| #3 | bounded audit sample of rule-backed rejections; `unsure` feedback |
| #4 | feedback→personalization bridge: latest answer per job, reversible |
| #5 | self-check CLI/API/Lab panel |
| #6 | refuse a non-loopback LLM endpoint unless `FYJ_ALLOW_REMOTE_LLM=1` |
| #7 | HelloWork entity-encoded JSON-LD |
| #8 | decision journal uses the API's run id |
| #9 | "3 à 8 ans minimum" requires 3, not 8 |
| #10 | readable stage names and job links on the Review page |
| #11 | LinkedIn/HelloWork scans manual by default |
| #12 | no France Travail credentials → that source skipped visibly |
| #13 | unqualified education level = uncertain |
| #14 | mandatory cue after "N ans d'expérience" |
| #15 | feedback coverage in the self-check and Lab |
| #16 | forwarding headers cannot bypass the local-only guards |
| #18 | look-alike DNS names are not loopback LLM endpoints |
| #19 | self-check wording matches what the lab agent tries |
| #20 | an unreadable HelloWork listing is not checked off |
| #21 | `python-jobspy>=1.3.0`; Python dependency audit in CI |
| #22 | a never-run scanner is not badged "Completed" |
| #23 | a rate-limited LinkedIn page is a failed query |
| #24 | HelloWork TELECOMMUTE at a workplace is hybrid |
| #25 | the lab CLI creates its bundle store on a fresh install |
| #26 | each source reported as completed / partial / failed / skipped; Indeed without its key skipped |
| #27 | Windows installer, desktop + Start Menu shortcuts, first-run guide, version 1.0.0rc1 |
| #28 | APEC's bot protection reported as one clear failure |
| #29 | build machine path kept out of the installer's bytecode |
| #17 | this record |

Stacked PRs (#5, #10, #13–#15, #18–#20) were brought up to date by merging `main` into their
branches (no force-push). Each conflict resolution was compared file by file with the
integration branch that had passed the full suite. #19 needed a correcting commit: a `-X ours`
merge had kept stale copies of files changed by #13 and #15. The comparison caught it before
the merge.

**Tree identity.** After #1–#25, `main`'s tree equalled the round-3 integration tree
(`f09f499`, full suite reproduced this round) except this file. After #26–#28, `main`
`3032d61` equalled the round-4 integration tree whose results are below.

## Test evidence (exact commands)

| What | Command | Result |
|---|---|---|
| Backend, round-3 integration `f09f499` | `pytest -m "not network"` | 5249 passed, 9 skipped, 0 failed |
| Backend, round-4 integration (= `main` `3032d61`) | `pytest -m "not network"` | **5280 passed, 9 skipped, 0 failed** |
| Frontend, round-4 integration | `npm run lint`; `npx tsc -b`; `npx vitest run` | 0 errors; OK; **120 passed** (19 files) |
| Hosted CI on `main` `3032d61` | Tests, Frontend, Lint, Secret scan, Dependency audit, Dependency graph | all success |
| Self-check on the merged code | `python -m src.evaluation self-check --no-feedback --no-write --markdown` | PASS: unsafe rejections 0, precision 0.88, recall 1.0, baseline identical, ranking `not_measured` |
| Installer rules | `pytest tests/test_windows_package.py tests/test_launcher.py` | all passed |
| Installer end to end | `packaging\windows\acceptance.ps1 -Installer …Setup-1.0.0rc1.exe` | **ACCEPTANCE PASSED, 60/60 checks, on the 3032d61 build and again on the final c86bba3 build** |
| Live security probes on the installed backend | see G | 10/10 passed |

The 9 skips are environmental: no local LLM on `:1234`, POSIX-permission cases on Windows,
and `tls_client`, which python-jobspy 1.3 no longer installs.

## Checklist

### A. Repository and integration

| Item | Status | Evidence |
|---|---|---|
| Every finished PR merged, none stranded | verified | table above |
| Stacked PRs revalidated on their new base | verified | CI green on each updated head; trees compared with the tested integration |
| Public/private boundary | verified | all work in `ItackFPS/Find-Your-Job-With-AI-Public`; no private-repo change |
| Branch protections respected | verified | ruleset "Protect main": PR + required checks; no bypass, no force-push |

### B. Installation and launch (Windows)

Installer `FindYourJobWithAI-Setup-1.0.0rc1.exe` built by `packaging\windows\build.ps1` from
`main` with Inno Setup 6.7.3 and a bundled CPython 3.12.10. Final artifact: built from `main`
`c86bba3`, 209,582,267 bytes, SHA-256
`2ba2f0d2c1aac23b61a720aecbbe7ab7e1010a8208c7b1f12575546564e4d2d2` (not published; built
locally). The walkthrough below used the `3032d61` build, which differs only in the bytecode
paths fixed by #29.

| Item | Status | Evidence |
|---|---|---|
| Per-user install, no admin rights | verified | `PrivilegesRequired=lowest`; silent `/CURRENTUSER` install exit 0; registered under HKCU |
| No dependence on a checkout or developer path | verified | runtime `<app>\runtime\python.exe` with a `._pth` (ignores `PYTHONPATH`/`PYTHONHOME`/user site); the build imports the backend from outside any checkout with both set to bogus paths; no `.venv` |
| Desktop and Start Menu shortcuts | verified | target `<app>\FindYourJob.cmd`, working folder `<app>`, app icon; Stop, logs and self-check entries; the desktop shortcut can be unticked |
| Launch from the desktop shortcut | verified | walkthrough: backend healthy on `127.0.0.1:8000`, dashboard opened |
| Health wait, single instance | verified | second launch exits 0 and reuses the backend (same pid, one backend process) |
| No scan at launch, restart or upgrade | verified | `/api/pipeline/runs` = `[]`; LinkedIn/HelloWork `last_scan: null`, `is_scanning: false` |
| Clean stop and restart | verified | Start Menu Stop → backend process gone (no orphan); relaunch → data intact |
| Port conflict / failed startup | verified (tests) | `tests/test_launcher.py` (foreign port refused, crash at startup reported with log lines) |
| Upgrade keeps data and settings | verified | upgrade while running: app stopped first; `jobs.db`, `profile.local.yaml` and an edited default config kept |
| Uninstall | verified | app files and shortcuts removed, unregistered; `data\`, `config\`, `logs\` kept (interactive uninstall asks, default No) |
| Startup time | measured | 41–76 s cold (precompiled bytecode; a fresh copy is also scanned by antivirus), about 20 s warm |
| Logs locatable, no secrets | verified | Start Menu → logs; walkthrough logs scanned: no credential values |
| Package free of secrets, databases and personal files | verified (#29) | the build refuses `.env`, `*.local.*`, databases, logs and `data\`; a scan of the stage found none. It found the build machine's checkout path in 14,019 `.pyc` files; #29 strips it and fails the build if any staged file contains it |

### C. First run and sources

| Item | Status | Evidence |
|---|---|---|
| First-run guidance | verified | Dashboard "Getting started" card while the database is empty; `docs/windows-installer.md` |
| Usable without credentials | verified | walkthrough run completed with no `.env` |
| Skipped vs failed vs completed | verified (#26) | live run: France Travail and Indeed *skipped* (with the variables to set), APEC *failed*, Jobicy, WWR, RemoteOK, EURES *completed* |
| APEC HTTP 403 | root cause found (#28) | DataDome bot protection: `403` with a `captcha-delivery.com` URL. The collector stops after one request and says so; it never tries to get round the CAPTCHA. APEC stays active in the shipped config (pinned by `tests/test_indeed_redundancy_coverage.py`): owner decision |
| Local-first | verified | AI off by default; non-loopback LLM endpoint refused (#6, #18); the first run downloads the embedding model from Hugging Face once |

### D. User walkthrough (installed app, real offers, 2026-10-19)

| Step | Result |
|---|---|
| Install, launch from the desktop shortcut | healthy, version 1.0.0rc1, no run, scanners idle |
| Profile edited in the UI (added a skill) | `PUT /api/profile` 200, saved to `config\profile.local.yaml` |
| Bounded manual run (**Run pipeline**, AI off) | completed in 121 s: 2568 collected, 2169 after dedup, 210 past the hard filter, 141 past the requirements gate, 141 scored |
| Jobs list | 141 jobs with scores and tiers; search filters correctly |
| Job detail | original offer and link, score, decision record (12 supporting, 0 against, 2 unknown; strategy and run id), "things to check", match factors |
| Feedback | "Interested" → `201`, stored with `decision_id`, `strategy_version`, `decision_run_id`; counted in the self-check as a signal |
| Self-check (Start Menu entry) | report written; Lab: pass, 0 unsafe, precision 88 %, recall 100 %, ranking not measured |
| Lab | no strategy promoted; the pipeline runs on `config/` |
| Stop (Start Menu) and relaunch (desktop) | 141 jobs, 1 run, feedback and profile kept; no new run or scan |
| Uninstall | data kept as documented |

### E. Decision safety and evaluation

| Item | Status | Evidence |
|---|---|---|
| No unsafe rejection on the benchmark | verified | self-check: 0 of 22 suitable jobs rejected |
| Mandatory vs preferred vs non-eliminatory education; ambiguity kept uncertain | verified | ADR 0004; #2, #13; `tests/test_rejection_safety_gate.py` in the required job |
| Experience minimums and ranges | verified | #9, #14 |
| Remote vs hybrid | verified | #24 |
| Decision provenance and strategy version | verified | job page decision record; feedback rows carry decision, strategy, run |
| Benchmark | reproduced | precision 0.88, recall 1.0, 3 FP (keyword traps bm31, bm36, bm39), 0 FN; synthetic only |
| Ranking metrics | **not measured** | no graded relevance labels |
| Real-world accuracy | **blocked** | needs human-reviewed labels |
| Thresholds, blacklists, promotions | unchanged | nothing tuned to move a metric |

### F. Lab

| Item | Status | Evidence |
|---|---|---|
| Weak evidence cannot be promoted; agents cannot approve | verified (round 3) | `insufficient_evidence` verdict; `approve` refused in an agent environment; `tests/test_lab_promotion_authority.py` |
| Lab cannot silently alter production | verified | active strategy = `config/` after every step; promotion only through `python -m src.lab approve` (typed confirmation) |
| OS-level isolation | **not enforced** | `python -m src.lab isolation-check` from the installed copy: `os_enforced: false`, exit 3. Cause: the agent runs under the user's own account. The dedicated account of ADR 0011 (`scripts/lab_isolation.ps1`) needs an administrator to create it, which is the owner's action. The Lab page and README say so |

### G. Security

| Item | Status | Evidence |
|---|---|---|
| Loopback binding | verified live | installed backend listens on `127.0.0.1` only; LAN, link-local and virtual adapter addresses refused |
| Host guard (DNS rebinding) | verified live | foreign `Host` → 400, also with `X-Forwarded-Host`/`X-Forwarded-For` |
| Origin guard (cross-site writes) | verified live | `Origin: https://evil.example` → 403, `Origin: null` → 403, also with forwarded headers |
| Dashboard path traversal | verified live | `/%2e%2e/%2e%2e/config/profile.yaml` does not serve the file |
| Same-origin UI writes on another port | verified | acceptance ran on `:8765` |
| Local LLM only, remote opt-in | verified | #6, #18 |
| Python dependencies | verified | `pip-audit` on `.[dev,linkedin]` and on the installer's pinned runtime set (`packaging/windows/requirements.txt`): no known vulnerabilities; both in CI |
| npm dependencies | verified | `npm audit`: 0 vulnerabilities |
| Secrets in the repository | verified | Gitleaks in CI; secret scanning and push protection on |

## Residual risks

- **Real-world accuracy and ranking quality are unmeasured.** All figures rest on 50 synthetic
  jobs (22 suitable).
- **Lab isolation is in-process only** unless the owner sets up ADR 0011's separate account.
- **Unsigned installer**: SmartScreen warns; users should check the published SHA-256.
- **Bundled CPython 3.12.10** is the build machine's; rebuild with the latest 3.12 patch release
  for its security fixes.
- **APEC** fails on every run while its bot protection lasts (visible, one request).
- **Deep install paths can break the LinkedIn extra** (`curl_cffi` DLL path over about 250
  characters). The default install path is far shorter.
- **`browser` extra** (crawl4ai, inactive, not bundled): `nltk` advisory PYSEC-2026-3740, not
  reachable from the app; not audited in CI.
- **Host guard accepts requests without a `Host` header** (HTTP/1.0 only; browsers always send
  one).
- **`apply_url` scheme is not validated by the backend**; the UI relies on React 19 blocking
  `javascript:` URLs.

## Owner actions

1. Decide whether to ship `apec` with `active: false` while it is blocked (needs updating the
   redundancy tripwire test).
2. Review real labels before any real-world metric is reported.
3. Optional: set up the lab agent's separate Windows account (`scripts/lab_isolation.ps1`).
4. Optional: code-sign the installer; publish a GitHub release with the `.exe` and `.sha256`.

## How to test it yourself

See `docs/windows-installer.md`. In short: run the setup `.exe`, keep the desktop shortcut,
double-click **Find Your Job With AI**, fill in the Profile, press **Run pipeline**.
