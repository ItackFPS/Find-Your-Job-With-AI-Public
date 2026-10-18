# V1 status record

Working record of where V1 stands, so work can resume after an interruption. It states only
what was verified, with the evidence; anything not re-run is said to be so. Update it when the
state changes. Last update: 2026-10-19.

**Verdict: V1 is not complete.** The code for every V1 area exists and the integrated branch
passes its full suite. Completion is blocked by decisions and work that only the owner can do:
merging the open pull requests, and reviewing real-world labels (see [Blockers](#blockers)).

Status words: **verified** (shown by tests or by running it), **implemented, not verified**,
**partial**, **blocked**, **not implemented**.

## Delivery state

Nothing below is on `main` yet. All pull requests are drafts with green CI
(`test`, `frontend`, `gitleaks`, `ruff`), stacked as follows; merge in this order and
retarget each child to `main` once its base has merged (`gh pr edit N --base main`).

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

Full suite on a local integration of #1–#15 (not pushed): **5237 passed, 8 skipped**
(2026-10-19; the #1–#10 integration alone: 5207 passed, 8 skipped).

## Checklist

### A. Repository and integration baseline

| Item | Status | Evidence |
|---|---|---|
| State of the public repository and PRs | verified | table above; `main` = `24cf085` |
| Conflicting / incorrectly based changes | verified | all 16 PRs merge cleanly together (local integration, suite green) |
| Preferred vs mandatory education policy | verified (pending merge) | ADR 0004 amendment (#2, #13); `tests/test_education_requirement_strength.py`, `tests/test_hard_filter_three_valued.py` |
| Unsafe-rejection gate active in CI | verified (pending merge) | `tests/test_rejection_safety_gate.py` runs in the required `test` job (#2) |
| Public/private boundary respected | verified | all work in clones of `ItackFPS/Find-Your-Job-With-AI-Public`; no private-repo change |
| Pre-existing failures | none found | full suite green before and after |

### B. Application experience

| Item | Status | Evidence |
|---|---|---|
| Windows install, launcher start/stop/restart | implemented, verified 2026-10-18 | release session ran `FindYourJob.cmd` live; not re-run this session |
| First run does not start collection unasked | verified (#11) | `tests/test_shipped_schedules.py`; live: backend started, 0 dispatches |
| Fresh install without credentials can run | verified (#12) | `tests/test_pipeline_preflight.py` |
| Dashboard, job list (filters, tiers, sort), job detail with original offer | verified | live run 2026-10-19 on a 30-offer test database |
| Explainable decision on the job page | verified | decision record: supporting / against / unknown evidence, confidence "not calibrated" |
| Feedback from the UI reaches storage | verified | live: "Interested" → `POST /feedback` 201 → explicit `feedback_events` row |
| Collection failures visible | partial | HelloWork zero-result detection (#7), preflight DEGRADED reasons (#12); no dashboard view of preflight status |
| Docs: install, usage, troubleshooting | implemented | README, `docs/windows-app.md`; not re-read end to end this session |

### C. Decision safety and explainability

| Item | Status | Evidence |
|---|---|---|
| No unsupported rejection of benchmark-suitable jobs | verified | self-check: unsafe rejections 0 / 22 suitable |
| Mandatory vs preferred vs non-eliminatory | verified | ADR 0004 table; tests above |
| Ambiguity kept as uncertainty | verified (#13) | an unqualified level above the ceiling → `ConstraintState.UNKNOWN`, never a reject |
| Eligibility separate from ranking | verified | ADR 0002/0003; self-check reports them apart |
| Rejections traceable to a rule and evidence | verified | self-check "rejections with a stated reason: 100%" |
| Rule-backed rejections can reach review | verified (#3) | audit sample, `rejection_audit_max_open=3` |
| Experience thresholds read correctly | verified (#9, #14) | ranges use the lower bound; cue after the noun is mandatory |
| Messages match decisions | verified (#13) | "(stated level, equivalence accepted)" instead of "(preferred)" |

### D. Feedback and personalization

| Item | Status | Evidence |
|---|---|---|
| Feedback persisted and traceable to job and decision | verified | live UI test; `tests/test_feedback_*` |
| Latest answer wins; repeated answers do not stack; clearing is scoped | verified (#4) | bridge rows `bridge:<event_id>` |
| Personalization opt-in and its off state | verified (#4) | README; tests |
| Feedback visible in the self-check | verified (#15) | Lab page "Your feedback (signals, not labels)" |
| Feedback improves ranking | **not measured** | no reviewed real dataset with graded labels; stored feedback is not proof of learning |

### E. Self-checking and evaluation

| Item | Status | Evidence |
|---|---|---|
| Reproducible dataset with provenance | verified | `synthetic-ai-filter-50`: 50 jobs, 50 human labels, hash-pinned |
| Versioned baseline, regression detection | verified (#5) | golden file; self-check status `regression`/`changed`; exit code |
| Safety gate fails on a deliberate unsafe rejection | verified (#2) | `tests/test_rejection_safety_gate.py` |
| Eligibility metrics | verified | precision 88%, recall 100%, FP 3, FN 0, unsafe 0 (synthetic only) |
| Ranking metrics | **not implemented** | dataset has eligibility labels, not graded relevance |
| Reviewed real-world dataset | **blocked** | 42 real decisions exported; labels not reviewed by a human |

### F. Controlled self-improvement

| Item | Status | Evidence |
|---|---|---|
| Patterns → hypotheses → experiments in the lab | verified | 2026-10-19: hypothesis "sales-engineer titles are pre-sales", candidate bundle, experiment `exp-cadaf5257e` |
| Same conditions, safety guardrails | verified | fixed 2, broken 0; guardrails no-new-unsafe, reproducible, latency all green |
| Weak evidence cannot be promoted | verified | verdict `insufficient_evidence` (2 discordant pairs, need 5), not eligible for approval |
| Human-only promotion, rollback | verified (tests) | ADR 0011; `approve` needs typed confirmation; `rollback`, `deactivate` |
| Agent cannot alter its own gates | partial | in-process write guard verified (refused `mkdir` outside the lab); OS-level isolation **not enforced** on this account (`isolation-check`: 31 writable targets) |

### G. Security

| Item | Status | Evidence |
|---|---|---|
| Binding | verified | backend and Vite bind `127.0.0.1` |
| DNS rebinding, cross-site writes | verified | `tests/test_api_host_guard.py`, origin guard tests |
| Forwarded headers | verified (#16) | `tests/test_api_forwarded_headers.py`: production app behind uvicorn proxy middleware |
| Local-first, no cloud fallback | verified (#6) | non-loopback LLM endpoint refused unless explicitly opted in |
| Secrets | verified | 0 secret-scanning alerts; Gitleaks in CI |
| Dependencies | partial | 0 Dependabot alerts; `npm audit` in CI; no Python audit in CI. pip-audit 2026-10-19: fresh install only `setuptools` 78.1.0 (build-time advisories). See residual risks |
| Lab isolation | partial | see F |

### H. Real-world validation

| Item | Status | Evidence |
|---|---|---|
| Bounded live collection | verified 2026-10-18 | 42 decisions on 30 offers (HelloWork, LinkedIn bounded scans) |
| Defects found on real offers | verified | fixed: #7 (HelloWork 0 jobs), #9 (range read as upper bound), #14 (2 senior offers passed), #13 ("Un Master" uncertainty hidden) |
| Labels reviewed by a human | **blocked** | owner review needed; no accuracy claim is made on real offers |

### I. Reliability and presentation

| Item | Status | Evidence |
|---|---|---|
| Tests, lint, build | verified | suite above; frontend lint 0 errors, build OK, 112 tests |
| V1 presentation | not done | to write once the PRs are merged |

## Residual risks

- **LinkedIn extra and markdownify.** `python-jobspy` 1.1.82 pins `markdownify<0.14`, which has
  PYSEC-2026-1604 (unbounded memory on `<h9999999>` tags in posting HTML). A fresh
  `pip install -e ".[linkedin]"` resolves `python-jobspy` 1.3.0 (released 2026-10-08,
  `markdownify>=1.1.0`), but the scanner has not been run against 1.3.0 and CI never installs
  the extra. Existing environments should upgrade once 1.3.0 is verified.
- **No Python dependency audit in CI.**
- **Lab agent isolation is in-process only** on an account that can write production state;
  ADR 0011's OS-level setup (`scripts/lab_isolation.ps1`) is not applied here.
- **Host guard accepts requests without a `Host` header** (HTTP/1.0). Browsers always send one,
  so this is not a rebinding path.
- **Accuracy claims rest on 50 synthetic jobs** (22 suitable). Nothing measures real-world accuracy
  or ranking quality yet.

## Blockers

1. **Merge the PRs** (owner): order #1 → #2, #3, #4, #6–#9, #11, #12, #16 → #5, #10, #13, #14 → #15.
2. **Review real labels** (owner): the 42 exported real decisions need a human accept/reject label
   before any real-world metric is reported.

## Next steps (not blocked)

- Self-check recommendation text claims the lab agent can attribute the keyword-trap pattern; on
  the current data it finds no shared title token and proposes nothing. Make the text conditional.
- Show the preflight status (READY / DEGRADED) per run on the dashboard.
- Add a graded-relevance dataset so ranking can be measured.
