# 0011 — Experiment Lab: human-approved strategy evolution

Status: accepted

## Context
Decisions are now recorded (ADR 0007–0010) and measurable (evaluation harness). Changing scoring or
filters must stay a measured, reversible, human-approved act.

## Decision
- `src/lab/` is never imported by production (tested). Strategies are **config bundles**
  (`filters.yaml` + `scoring.yaml`, deep-merged data overrides, content-addressed, read-only files):
  a candidate is data, never code.
- State lives in a separate `data/lab.db` (hypotheses, candidates, immutable experiments,
  append-only approvals). Nothing in the lab opens `jobs.db` for writing except `promotion`.
- That isolation is **enforced by the process**, not by convention: every `python -m src.lab`
  command except `approve`/`rollback` (and any lab automation) installs `src/lab/guard.py`, a CPython
  audit hook that, for the rest of the process, refuses writes anywhere in the repository except
  `lab.db` and the bundle store (so not `jobs.db`, `config/` — the active profile and production
  configuration — `src/` or `.github/`), refuses read-write SQLite connections to anything else
  (read-only `mode=ro` URIs stay allowed), refuses `ATTACH` / `VACUUM INTO` / extension loading on
  every SQLite connection (SQLite opens those files itself, unseen by audit hooks), resolves path
  aliases (`..`, case, `\\?\` prefixes, junctions, other names of the project root, hard links to a
  protected file) before checking, refuses starting processes (including `_winapi.CreateProcess`), and
  refuses network access and name lookups other than loopback. The lab CLI allows no loopback port
  either: the lab runs offline, and loopback would reach the application's own API, which can start
  pipeline runs or edit the profile. It cannot be widened through its API once installed. It guards
  the lab's code paths (which run no dynamic code: candidates are data) against writing production
  state by mistake. It is **not a sandbox**: code running inside the guarded process can disable it
  with ordinary Python (e.g. clearing the guard's state, or `Connection.set_authorizer(None)` before
  `ATTACH`) or native code (`ctypes`), and a process that never installs it — an agent's own shell, or
  the human `approve`/`rollback`/`deactivate` commands — is not restricted. Who may promote is decided
  elsewhere: see **Promotion authority** below.
- An experiment replays baseline and candidate (each twice, interleaved) on a frozen dataset and issues a verdict:
  `improvement | regression | inconclusive | insufficient_evidence`. Evidence floor: ≥30 labelled
  non-borderline jobs and ≥5 discordant pairs; otherwise `insufficient_evidence`. `improvement`
  needs the paired-bootstrap 95 % CI of the accuracy difference strictly above 0.
- Two tiers, never conflated. `eligible_for_approval` (an `improvement` with every guardrail green, on all
  labels) means *worth a human's review*. `promotion_evidence` says what supports it:
  `independently_supported` only when the independent `human` labels alone — assistant-assisted
  (`human_assisted`, or `human` with a proposal on record) and feedback-`derived` labels excluded — clear
  the same floor (≥30 labelled, ≥5 discordant) and CI test; otherwise `exploratory_only`
  (`not_eligible` when not eligible). `independently_validated` is true only for the first, the verdict's
  explanation says "EXPLORATORY ONLY" for the second, and the Lab page, agent report and API
  show the tier; experiments recorded before it count as `exploratory_only`. No specification
  or ADR requires independent evidence for promotion (the docs require disclosure and a human decision),
  so exploratory evidence stays approvable, but only with `--accept-exploratory-evidence`, recorded in
  the pointer's `approval_evidence` (`acknowledged_exploratory`) after the approver has been shown the
  tier. While it is live, `status`, `GET /api/lab/active-strategy` (`pipeline.approval_basis`) and
  the Lab page keep saying so (`exploratory_only`; `unrecorded` for a pointer without evidence), and
  a rollback carries the restored approval's basis instead of upgrading it.
- **Decision (2026-10-14): independent support is not required for promotion — yet.** Requiring it
  now would make every candidate unapprovable: no dataset qualifies (`real-v1@2`'s accept labels are all
  assistant-assisted and it has no rejected sample), and the lab's value is precisely to try reversible,
  measured changes. What keeps that honest is that exploratory evidence can never pass for validation:
  it needs a typed acknowledgement, is recorded as such, and stays labelled while live; promotion stays
  reversible (`rollback`, `deactivate`), and every other gate (guardrails, freshness, recomputation)
  applies unchanged. *Independent evidence* means a `human` label from a person's own judgement with
  no assistant proposal on record or a blind decision (`annotation_assist show --blind` + `record
  --blind`); `human_assisted` and feedback-`derived` labels are not. The path to it is a **new** blind
  export (`docs/development.md` → *Collecting independent evidence*: `export --n 200 --rejected 100`,
  hand labelling without proposals, `annotate-import`, `audit --db`); historical versions such as
  `real-v1@2` are never relabelled. **Revisit** — and make `independently_supported` mandatory in
  `approve` — once one benchmark reaches the experiment floor on independent labels alone (≥30 labelled
  non-borderline jobs, with enough accept labels that recall is not resting on a handful), or as soon as
  an exploratory approval turns out to have harmed production decisions.
- Guardrails (any failure ⇒ no approval, verdict `regression`): no *new* unsafe rejection versus
  the baseline (absolute counts reported), identical decisions across two candidate runs,
  latency within tolerance. Latency is measured on comparable work (`src/lab/latency.py`): each replay
  times its stages (filter, requirements gate, scoring, assessment of accepted and of rejected jobs) and
  records the jobs each stage processed; the guardrail compares the candidate's stage time with what the
  baseline's per-job stage costs predict for the candidate's workload (`workload_adjusted_ratio` ≤ 1.5,
  + 2 ms absolute), so accepting more jobs is not mistaken for being slower, while the aggregate ratio and
  the jobs per stage are reported beside it so extra work never looks free. Each side is replayed twice,
  interleaved, and each stage's fastest replay counts: the first replay in a process pays a cold start
  (≈300 ms/job against ≈2.6 ms warm) that previously fell on the baseline and could hide a slower candidate.
  On `synthetic-ai-filter-50` the agent test's fix measures ≈0.96–1.06× workload-adjusted (≈1.7×
  aggregate, the former measure, which failed ≈1 run in 30–40). The filter's per-job ratio stays ≈1.6×
  there because a job that passes runs every check and a rejected one stops at the first: within-stage
  cost still depends on outcomes, so a regression confined to a cheap stage can stay under the overall
  tolerance; per-stage ratios are reported, not gated.
- Each verdict carries a `comparison`: precision, recall, F1, unsafe-rejection rate, reject recall,
  review (abstention) rate and confusion counts for baseline and candidate with their deltas,
  recomputed from the judged decisions, plus the label provenance and the comparison's limitations
  (non-independent labels, few accept labels, survival-biased sampling). Reported, never sufficient
  for approval on its own. It also carries `ranking` (AUC, precision@k and the `relevant` visibility
  floor over the accepted jobs; the AUC change and its paired bootstrap CI are computed on the jobs
  both sides accept, with the jobs only one side accepts counted and the most-moved jobs listed): a `scoring.yaml`-only
  candidate flips no gate decision, so the verdict is `insufficient_evidence` and only the ranking
  shows its effect. The ranking does not enter the verdict or eligibility.
- Promotion: `python -m src.lab approve <experiment> --dataset <its dataset> --by <name> --reason …`
  re-verifies the bundle hash and that it still reproduces from the current `config/`, then **recomputes
  the evidence** instead of reading it from `lab.db`: the dataset must load with its content hashes, be
  the one the experiment recorded and lie outside the lab's bundle store; baseline (what production runs
  now) and candidate are replayed in the approving process; the fresh baseline and candidate fingerprints
  and verdict must match the record (a mismatch means the record was altered or production changed:
  re-run), and the fresh verdict must be an eligible `improvement`. The approver is shown the recomputed
  verdict, tier and limitations and the change itself — a diff of what production runs now against the
  verified bundle, never the lab's or a proposer's description of it — and types back a one-time code;
  the pointer then stores `approval_evidence` (recomputed verdict, CI, tier, dataset path and hashes,
  fingerprints, that diff as `change`, reason, approver name + OS account + host). `rollback` appends a pointer to an earlier bundle, `deactivate` a
  pointer back to `config/`, both confirmed the same way and recorded with the approver's identity. No
  automatic promotion exists.
- **Production honours the pointer** through `src/decisions/active_strategy.py` (production still never
  imports `src.lab`; the bundle format is shared in `src/decisions/bundle_format.py`). At the start of
  each `run_pipeline` the latest row is resolved: `active` runs the bundle's `filters.yaml` and
  `scoring.yaml` (profile, semantic and personalization config still come from `config/`); `default`
  (no pointer), `deactivated`, `inert`, `invalid` (missing/modified bundle) and `stale` (the bundle's
  `overrides.json` applied to the current `config/` no longer reproduces it: the approved combination was
  never measured) all run on `config/`. The result is in `PipelineStats.active_strategy`, the decision
  journal's strategy manifest hashes the files actually run and records the pointer, and
  `python -m src.lab status` / `GET /api/lab/active-strategy` show it.
- Pointers carry `pipeline_effective`: rows written before the pipeline honoured pointers have 0 and
  stay `inert`, so an approval given when it had no effect never starts having one silently; approving
  again makes it effective.
- `--base-config active` (the lab default) replays the strategy production actually runs, and a
  candidate built on an active bundle records its overrides relative to `config/`.

- **Agent and memory** (`src/lab/agent.py`, `src/lab/memory.py`; `python -m src.lab agent|memory`):
  a deterministic investigator that runs only under the guard. It attributes each missed
  accept-labelled job to the exact `filters.yaml` keyword responsible (re-running the real check with
  the keyword removed), records rejections it has no data lever for (profile-driven freshness,
  location, salary; code-defined gate rules) as findings for a human, mines false-positive title
  tokens that occur in no accept/borderline title, turns each actionable finding into one data-only
  hypothesis + bundle, runs the standard experiment, and stores evidence / hypotheses / observations /
  conclusions in `lab.db` (immutable items, append-only status history, inputs hashed so items go
  stale when the dataset or strategy changes; identical inputs are never re-tested). An eligible
  improvement becomes a recommendation naming the human approve command; it never approves (the guard
  would refuse the `jobs.db` write anyway). No LLM is involved, so nothing leaves the machine.

## Promotion authority

Execution model (checked on the development machine, October 2026): automated agents (Claude Code,
Codex) run as the same Windows account as the human, without an OS sandbox, with a shell and full
write access to the repository, `data/` and `config/`. Their shells have a console (`CONIN$` opens and
`stdin.isatty()` is true on the NUL device), and anything that can write `config/filters.yaml` changes
production's strategy without any pointer at all. No check inside this repository can stop an account
from doing what the OS lets it do, so:

- **The boundary is the OS account.** The human promotes from an account that can write `jobs.db`; an
  agent must run under one that cannot write `data/jobs.db`, `config/`, `src/`, `scripts/`, `.github/`,
  `data/benchmarks/`, `.env*`, `Dockerfile`, `docker-compose.yml` or `pyproject.toml`, and can write
  only the lab database and `data/lab/strategies` (and cannot delete or rename `jobs.db`: no
  delete-child right on `data/` on Windows, the sticky bit on POSIX), and cannot read the secrets
  (`.env*`, `config/*.local.*`: `.env`'s n8n API key reaches the API through n8n).
  File permissions alone are not enough: **the API runs as the human and writes production state for any
  caller** (`PUT /api/profile`, `POST /api/pipeline/run`, …). With `API_SAME_USER_ONLY=1`
  (`src/api/peer_guard.py`, opt-in) it asks the OS which process owns each loopback connection and
  serves only its own account, failing closed on anything it cannot attribute — including any request
  with a forwarding header, since uvicorn by default rewrites the client address from
  `X-Forwarded-For` and an agent could otherwise claim one of the human's open connections (reproduced,
  then fixed); services of the human's account that relay to it (Vite's proxy, n8n) must not run while
  the agent does, since they are served as the human. **Neither is enough while the account can write
  anything the human executes**: where a drive root grants Authenticated Users Full
  Control (as on the development machine),  a PATH entry (`git.exe`) or a scheduled task's script there would run the agent's code as
  the human. `isolation-check --exec-path …` probes those places from the agent's account
  (`lab_isolation.ps1 -Mode Audit` lists them), and `-DenyRoot D:\` extends the deny to the drive.
  Setting that up (a local account plus ACLs) changes OS security settings and is not
  automated: `scripts/lab_isolation.ps1` plans, applies (exact access masks — `icacls /deny` adds
  `SYNCHRONIZE`, which also blocks reads) and audits the ACLs, including ownership (an owner keeps an
  implicit `WRITE_DAC`), disabled inheritance, explicit child Allows and links leading outside the
  repository. `python -m src.lab isolation-check`, run from the agent's account, probes every target
  without changing content and exits 3 unless none is writable, no secret is readable, the API port
  answers with the guard's refusal (nothing listening is unverified) and no relay port answers;
  `agent --require-isolation` refuses to run otherwise, and without a lab database under `data/lab/`.
  Without that setup "only a human promotes" is **procedural**.
- **Decision (2026-10-14): next step is a dedicated standard (non-admin) local account** for any
  automated agent that operates the lab — chosen over a container because the agents (Claude Code,
  Codex) are Windows tools that would otherwise need their own image, credentials and mounts, while an
  account boundary is enforced by NTFS for every process the agent starts. The agent keeps its lab state
  in `data/lab/` (`--lab-db data/lab/lab.db`; the Lab page reads it there when `data/lab.db` is absent),
  so its account needs write access to that one directory and nothing beside `jobs.db`. The human keeps
  the only account that can write `jobs.db` and therefore the only promotion path; the agent account
  must not be an administrator (otherwise it can rewrite the ACLs) nor know the human's password.
  The exact commands, and the check that proves them, are in `docs/development.md` → *Isolated lab
  agent*. A container with the repository mounted read-only, `data/lab` read-write and
  `--network none` is an equivalent alternative where the agent can run inside it.
- **Decision (2026-10-23): a Windows AppContainer is the default boundary; the separate account stays
  an option.** The account needs an administrator to create, which most users of the installed app
  are not willing to do for an optional tool. An AppContainer needs neither elevation nor a second
  account, and Microsoft services it as a security boundary. Every access must also be granted to the
  container's SID, so the user's own ACLs (and a drive root open to Authenticated Users) grant it
  nothing. It has no network unless `internetClient` is given, and never loopback unless an
  administrator exempts it, so the API and relays are out of reach without `API_SAME_USER_ONLY`.
  `src/lab/sandbox.py` (`python -m src.lab sandbox grant|run|revoke`) grants it read on the application
  folder and its Python and modify on `data/lab/` only. A deny ACE for an AppContainer SID is ignored
  (measured), so secrets are kept closed by never being granted: the root and `config/` pass only a
  folder grant down. `isolation-check` run inside reads a dead loopback port as `blocked` (the OS's
  refusal) and counts only an *enabled* Administrators membership. `tests/test_lab_sandbox.py` starts
  a real container and checks every refusal and the `os_enforced: true` verdict from inside. What it
  does not cover: an agent tool that needs files outside the granted folders must have them granted
  (`--grant-read`), and the lab can still read production data (by design, for replays).
- **What is enforced in code, whatever the account:** the approval path never trusts `lab.db` (above),
  so an agent that may write `lab.db` — by design, in the isolated setup — cannot manufacture the
  verdict, eligibility or tier that approval consumes; bundles are content-addressed and re-verified by
  production at every run (a swapped bundle is `invalid`, production falls back to `config/`); datasets
  are content-hashed and a dataset inside the bundle store is refused.
- **Procedural only (defence in depth, not a boundary):** `approve` / `rollback` / `deactivate` refuse
  to run inside the lab guard, with an agent marker in the environment (`FYJ_LAB_AGENT`, `CLAUDECODE`,
  `CODEX_SANDBOX*`) or without an interactive terminal, and are confirmed by a one-time code typed back;
  the former `--yes` is refused. That stops an agent from promoting through the supported command by
  habit or instruction-following; one that emulates a terminal, scrubs its environment, calls
  `src.lab.promotion` from Python, or writes `jobs.db` / `config/` with `sqlite3` or a text editor is not
  stopped by it — only by the OS boundary. `approval_evidence.approver.os_account` is what the OS
  environment reports, trustworthy only as far as the account boundary is.
- **Residual limitations, even with the boundary:** the approver chooses which dataset to re-check
  against (its path, id, label basis and tier are shown); labels outside `data/benchmarks/` are only as
  trustworthy as whoever can write them; the latency guardrail is recomputed too, so wall-clock noise can
  make a fresh replay disagree with the record (approval then refuses, fail-closed: re-run).

## Consequences
An approved bundle changes production decisions from the next run on, and editing `config/filters.yaml`
or `config/scoring.yaml` afterwards makes it `stale` (production returns to `config/`, logged as an error)
until the experiment is re-run and approved again. Every production reader of `filters.yaml` /
`scoring.yaml` goes through `src/decisions/active_strategy.py`: the pipeline and the LinkedIn
pre-staging filter (`src/linkedin/scanner.py`, which screens jobs for the next run) use the strategy
in effect now; the job-detail explanation helper uses the strategy the run that decided recorded in
`pipeline_runs.stats_json` (`active_strategy`), falling back to `config/` — with a warning — when that
bundle is gone or modified. `profile.yaml`, `semantic_config.yaml`, `personalization.yaml` and the
other `config/` files are not part of a bundle and always come from `config/`; editing the profile does
not make a bundle stale. Datasets from `feedback-dataset` below the P4
thresholds yield `insufficient_evidence`, so no candidate can currently be approved from
synthetic data alone — by design.
