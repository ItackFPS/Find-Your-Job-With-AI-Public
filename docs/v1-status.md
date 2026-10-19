# V1 status record

This is the working record of where V1 stands, so work can resume after an interruption. It states
only what was verified, with the evidence; anything not re-run says so. Update it when the state
changes. Last update: 2026-10-19 (round 3).

**Verdict: V1 READY WITH EXPLICIT LIMITATIONS, once the pull requests below are merged.** The
integrated branch (all 25 PRs) passes its full suite, a clean install, a fresh-launch end-to-end
run on real offers, the self-check and the lab loop. Nothing below is on `main` yet. The
limitations are listed under [Residual risks](#residual-risks). The main ones:

- Real-world accuracy is not measured: no human-reviewed real labels exist.
- Ranking quality is not measured: there are no graded relevance labels.
- Lab isolation is in-process only on this account.

Status words: **verified** (shown by tests or by running it), **implemented, not verified**,
**partial**, **blocked**, **not implemented**.

## Delivery state

All pull requests are drafts with green hosted CI (`test`, `frontend`, `gitleaks`, `ruff`; #21
adds `python-audit`), stacked as follows. Merge in this order and retarget each child to `main`
once its base has merged (`gh pr edit N --base main`).

| PR | Base | Change |
|---|---|---|
| #1 | main | required `frontend` check runs on every PR |
| #2 | #1 | a wished-for education level is not a ceiling (unsafe rejection bm46 fixed); CI safety gate |
| #3 | #1 | bounded audit sample of rule-backed rejections in the review queue; `unsure` feedback |
| #4 | #1 | feedback→personalization bridge: latest answer per job, reversible |
| #5 | #2 | self-check CLI/API/Lab panel; experiment next steps |
| #6 | #1 | refuse a non-loopback LLM endpoint unless `FYJ_ALLOW_REMOTE_LLM=1` |
| #7 | #1 | HelloWork entity-encoded JSON-LD (collector silently returned 0 jobs) |
| #8 | #1 | decision journal uses the API's run id |
| #9 | #1 | "3 à 8 ans minimum" requires 3, not 8 (real unsafe rejection) |
| #10 | #3 | readable stage names and job links on the Review page |
| #11 | #1 | LinkedIn/HelloWork scans manual by default (no scan at first start) |
| #12 | #1 | no France Travail credentials → that source skipped visibly, run continues |
| #13 | #2 | unqualified education level = uncertain; rejection messages match the decision |
| #14 | #9 | mandatory cue after "N ans d'expérience" / "N years of experience" |
| #15 | #5 | feedback coverage in the self-check and the Lab page |
| #16 | #1 | forwarding headers cannot bypass the local-only guards (tests) |
| #17 | #1 | this record |
| #18 | #6 | `127.example.com` / `127.0.0.1.nip.io` no longer pass as loopback LLM endpoints |
| #19 | #5 | self-check no longer promises a lab candidate it does not find |
| #20 | #7 | an unreadable HelloWork listing is not checked off; error reasons kept and shown |
| #21 | #1 | `python-jobspy>=1.3.0` (markdownify advisory), `fetch_description`; Python dependency audit in CI |
| #22 | #1 | a never-run scanner is no longer badged "Completed" |
| #23 | #1 | a rate-limited (HTTP 429) LinkedIn page is a failed query, not "exhausted" |
| #24 | #1 | HelloWork TELECOMMUTE at a workplace is hybrid, not fully remote |
| #25 | #1 | the lab CLI can create its bundle store on a fresh install |

Merge order: #1 → #2, #3, #4, #6–#9, #11, #12, #16, #17, #21–#25 → #5, #10, #13, #14, #18, #20
→ #15, #19. #18 needs #6; #20 needs #7; #19 needs #5.

Full suite on the local integration of all 25 PRs (`integration/v1-round3` at `f09f499`, not
pushed), clean venv with the `dev`, `linkedin` and `mcp_admin` extras: **5249 passed, 9 skipped, 0
failed** (`pytest -m "not network"`, 2026-10-19). The skips are environmental: no local LLM on
`:1234`, POSIX-permission and launcher cases on Windows, and `tls_client`, which python-jobspy 1.3
no longer installs.

## Checklist

### A. Repository and integration baseline

| Item | Status | Evidence |
|---|---|---|
| State of the public repository and PRs | verified | table above; `main` = `24cf085` |
| Conflicting / incorrectly based changes | verified | all 25 PRs merge cleanly together (local integration) |
| Preferred vs mandatory education policy | verified (pending merge) | ADR 0004 amendment (#2, #13); `tests/test_education_requirement_strength.py`, `tests/test_hard_filter_three_valued.py` |
| Unsafe-rejection gate active in CI | verified (pending merge) | `tests/test_rejection_safety_gate.py` in the required `test` job (#2) |
| Public/private boundary respected | verified | all work in clones of `ItackFPS/Find-Your-Job-With-AI-Public`; no private-repo change |
| Pre-existing failures | none found | full suite green |

### B. Application experience

Fresh-launch run on 2026-10-19, integrated branch, empty `data/`, shipped example profile, backend
on `127.0.0.1:8765`:

| Item | Status | Evidence |
|---|---|---|
| First start does not scan | verified (#11) | schedules seeded `startup_policy: disabled`; after 75 s (> 1 scheduler tick): no execution, no scan log line, no pipeline run |
| Upgraded install does not scan | verified (#11) | schedule rows forced back to `run_once_per_day` in the DB, restart → reset to `disabled` from the YAML, 0 dispatches after a tick |
| Restart after a hard kill | verified | backend killed (`taskkill /F`) three times; each restart recovered; jobs, run and feedback intact; 0 dispatches |
| Manual, bounded scans from the UI | verified | "Scan now" on HelloWork (3 roles × ≤3 jobs) and LinkedIn (2 queries × 5 results); progress and Pause shown while running |
| Fresh install without credentials can run | verified (#12) | pipeline run completed; France Travail and Indeed skipped with setup guidance in the run's errors |
| Source failures visible | verified | run errors list APEC `403 Forbidden` per keyword; HelloWork scan detail lists its error reasons (#20); LinkedIn failed queries shown (#23) |
| Dashboard, job list (filters, tiers, sort), job detail with original offer | verified | 67 jobs listed with tiers; job detail shows the posting and a link to it |
| Explainable decision on the job page | verified | decision record: 11 supporting / 0 against / 3 unknown / 1 conflict, strategy `sv-f066dc7bc80db1fb`, decision and run id, confidence "not calibrated" |
| Feedback from the UI reaches storage | verified | "Interested" → Save → `POST /feedback` → row with `decision_id`, `strategy_version`, `decision_run_id`; survives restart |
| Windows launcher (`FindYourJob.cmd`) | implemented, verified 2026-10-18 | not re-run this round (backend started with uvicorn directly) |
| Docs: install, usage, troubleshooting | implemented | README, `docs/windows-app.md`; not re-read end to end |

### C. Decision safety and explainability

| Item | Status | Evidence |
|---|---|---|
| No unsupported rejection of benchmark-suitable jobs | verified | self-check: unsafe rejections 0 / 22 suitable |
| Mandatory vs preferred vs non-eliminatory | verified | ADR 0004 table; tests above |
| Ambiguity kept as uncertainty | verified (#13) | an unqualified level above the ceiling → `ConstraintState.UNKNOWN`, never a reject |
| Eligibility separate from ranking | verified | ADR 0002/0003; self-check reports them apart |
| Rejections traceable to a rule and evidence | verified | self-check "rejections with a stated reason: 100%"; live: 1012 decisions with evidence |
| Rule-backed rejections can reach review | verified (#3) | audit sample, `rejection_audit_max_open=3` |
| Experience thresholds read correctly | verified (#9, #14) | ranges use the lower bound; cue after the noun is mandatory |
| Messages match decisions | verified (#13) | "(stated level, equivalence accepted)" instead of "(preferred)" |
| Explanations state true facts | fixed (#24) | live: 3/3 HelloWork "Télétravail partiel" offers were labelled fully remote and explained as matching a remote preference |
| Synthetic false positives bm31, bm36, bm39 | investigated, not changed | "keyword traps": the postings say they are sales / business-analyst roles. They are kept (the safe direction). A deterministic fix would be a title or keyword exclusion, which the policy rules out without evidence; see F |

### D. Feedback and personalization

| Item | Status | Evidence |
|---|---|---|
| Feedback persisted and traceable to job and decision | verified | live UI test above; `tests/test_feedback_*` |
| Latest answer wins; repeated answers do not stack; clearing is scoped | verified (#4) | bridge rows `bridge:<event_id>` |
| Personalization opt-in and its off state | verified (#4) | README; job page says feedback "does not change your ranking yet" |
| Feedback visible in the self-check | verified (#15) | live: `jobs_with_explicit_feedback: 1`, `latest_answers: {interested: 1}`, reported as "signals, not labels" |
| Feedback improves ranking | **not measured** | no reviewed real dataset with graded labels |

### E. Self-checking and evaluation

| Item | Status | Evidence |
|---|---|---|
| Reproducible from a clean checkout | verified | `python -m src.evaluation self-check` on the integrated branch: PASS, baseline identical |
| Reproducible dataset with provenance | verified | `synthetic-ai-filter-50`: 50 jobs, 50 human labels, hash-pinned |
| Versioned baseline, regression detection | verified (#5) | golden file; status `regression`/`changed`; exit code |
| Safety gate fails on a deliberate unsafe rejection | verified (#2) | `tests/test_rejection_safety_gate.py` |
| Eligibility metrics | verified | precision 0.88, recall 1.0, FP 3, FN 0, unsafe 0 (synthetic only) |
| Honest recommendations | verified (#19) | the lever text says what the lab agent tries; live agent run: no findings, matching the text |
| Ranking metrics | **not implemented** | dataset has eligibility labels, not graded relevance |
| Reviewed real-world dataset | **blocked** | needs a human; see H |

### F. Controlled self-improvement

Live loop on 2026-10-19 (integrated branch, fresh `data/`):

| Item | Status | Evidence |
|---|---|---|
| Frozen baseline | verified | lab `status`: no pointer, production = the configuration directory; `config/*.yaml` hashed before |
| Pattern → hypothesis → candidate | verified | `hyp-daae16045e` ("sales-engineer titles are pre-sales", bm31/bm39), `cand-2b6a207dd1e9` (title exclusion "sales engineer") |
| Same dataset, baseline vs candidate | verified | `exp-648ebb346b`: fixed 2, broken 0; precision 0.88 → 0.957, recall 1.0 → 1.0, unsafe 0 → 0; guardrails no-new-unsafe, reproducible, latency (ratio 0.91) all passed |
| Weak evidence cannot be promoted | verified | verdict `insufficient_evidence` ("2 discordant pairs (need 5)"), `eligible_for_approval: false`; `tests/test_lab_promotion_authority.py::test_an_honestly_ineligible_experiment_cannot_be_approved` |
| An agent cannot approve | verified | `approve` (with and without `--accept-exploratory-evidence`) → "refusing approve: automated-agent environment", exit 2 |
| Production unchanged | verified | pointer still null; `config/*.yaml` byte-identical after the loop |
| Fresh-install lab | fixed (#25) | `candidate` failed on a fresh install (guard refused `data/lab`) |
| Agent cannot alter its own gates | partial | in-process guard verified; OS-level isolation **not enforced** on this account (the agent reports 34 writable production targets) |

### G. Security

| Item | Status | Evidence |
|---|---|---|
| Binding | verified | backend and Vite bind `127.0.0.1` |
| Host / Origin guards cover every route | verified (code review) | both are app-wide ASGI middlewares (all routers, `/assets`, the SPA catch-all, the SSE stream); every write route lives under `/api`; no GET route writes; no WebSocket route |
| DNS rebinding, cross-site writes, forwarded headers | verified | `tests/test_api_host_guard.py`, origin guard tests, `tests/test_api_forwarded_headers.py` (#16) |
| SPA path traversal | verified (code review) | `spa_file` resolves and requires `is_relative_to(dist)` |
| Untrusted posting content in the UI | verified (code review) | no `dangerouslySetInnerHTML`; posting text rendered as text; `apply_url` goes into an `href`, where React 19 blocks `javascript:` URLs. There is no backend scheme check |
| Local-first, no cloud fallback | verified (#6, #18) | non-loopback LLM endpoint refused unless opted in; look-alike DNS names now refused too |
| Secrets | verified | 0 secret-scanning alerts; Gitleaks in CI |
| Python dependencies (fresh install) | verified (#21) | clean venv `.[dev,linkedin]`: `python-jobspy` 1.3.0, `markdownify` 1.2.3, pip-audit "No known vulnerabilities found" (82 packages, locally and in hosted CI run 39182024433) |
| Python audit in CI | verified (#21) | `Dependency audit` workflow: PRs, `main`, weekly |
| npm dependencies | verified | `npm audit --omit=dev --audit-level=high` in the required `frontend` job |
| Lab isolation | partial | see F |

### H. Real-world validation

| Item | Status | Evidence |
|---|---|---|
| Bounded live collection, fresh install | verified 2026-10-19 | 1012 offers collected, 981 after dedup, 92 past the hard filter, 67 past the requirements gate, 67 scored (sources: WWR 33, EURES 22, RemoteOK 6, HelloWork 3, Jobicy 3); LinkedIn 5 fetched, all pre-filtered |
| Real-source defects found and fixed | verified | earlier rounds: #7, #9, #13, #14. This round: #20 (unreadable listing checked off), #23 (HTTP 429 read as exhausted), #24 (partial telework read as remote), #22 (misleading badge) |
| python-jobspy 1.3.0 against real LinkedIn | verified | 3 bounded calls: every column the scanner reads present, descriptions returned with `fetch_description`; no crash when loaded after torch + scikit-learn |
| Labels reviewed by a human | **blocked** | none of the real decisions has an independent human label; no accuracy claim is made on real offers. The one "interested" answer in the test database was entered during the UI test, not by the owner |

### I. Reliability and presentation

| Item | Status | Evidence |
|---|---|---|
| Backend tests | verified | integration of all 25 PRs: 5249 passed, 9 skipped (environmental), 0 failed |
| Frontend | verified | lint 0 errors (3 pre-existing warnings), `tsc -b` OK, build OK, 108 tests (with #22) |
| Hosted CI | verified | every PR #1–#25 green |
| V1 presentation | not done | to write once the PRs are merged |

## Residual risks

- **Real-world accuracy and ranking quality are unmeasured.** All accuracy figures rest on 50
  synthetic jobs (22 suitable). Ranking has no graded labels.
- **Lab agent isolation is in-process only** on an account that can write production state.
  ADR 0011's OS-level setup (`scripts/lab_isolation.ps1`) is not applied here.
- **APEC answers 403 to every search** (2026-10-19). The failure is visible in the run's errors
  and the source contributes nothing. It is not investigated here.
- **Deep Windows install paths break the LinkedIn extra.** `curl_cffi`'s bundled DLL fails to load
  when its path exceeds about 250 characters ("file name too long").
- **`browser` extra** (crawl4ai, inactive by default) pulls `nltk` 3.10.3 with PYSEC-2026-3740
  (unpatched; NLTK model save/load with caller paths). The app never calls those APIs, so the
  advisory does not apply today. The extra is not audited in CI.
- **HelloWork full remote is under-claimed** (#24). A fully remote job whose description does not
  say so is now `hybrid`.
- **Host guard accepts requests without a `Host` header** (HTTP/1.0). Browsers always send one, so
  this is not a rebinding path.
- **`apply_url` scheme is not validated by the backend.** The UI relies on React 19 blocking
  `javascript:` URLs.

## Blockers

1. **Merge the PRs** (owner), in the order above.
2. **Review real labels** (owner), before any real-world metric is reported.

## Next steps (not blocked)

- Small graded-relevance dataset (for example 30 real offers, rated 0–3 by the owner) so ranking
  can be measured.
- Read HelloWork's telework badge directly; count detail pages without JobPosting JSON-LD.
- Investigate the APEC 403.
- Remove the dead `tls_client` preload once #21 is merged.
- The LinkedIn per-page log line uses `→`, which is lost on a cp1252 stderr.
