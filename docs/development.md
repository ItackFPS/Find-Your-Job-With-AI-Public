# Development Workflow

How changes get from an idea onto `main`. The **rules** (branch names, commit
granularity, scope control, when to stop) live in
[`CLAUDE.md` → Git & GitHub Workflow](../CLAUDE.md#11-git--github-workflow) and apply
to humans and agents alike. This file is the practical companion: setup, commands,
CI, and troubleshooting.

```text
Issue → branch → plan → small commits (tests green) → push → draft PR → CI
      → self-review → ready → squash-merge → issue closed
```

## One-time setup (per clone)

### 1. Install the pre-push guard

[`.githooks/pre-push`](../.githooks/pre-push) refuses any push that updates or deletes
the remote `main`, so the mistake is caught before anything leaves your machine. On
this public repository GitHub also enforces it (see the *Repository protections* list
in step 3); on the private development repository this one is exported from, which
has no branch protection on GitHub's Free plan, the hook is the only guard.

```bash
git config core.hooksPath .githooks
```

Verify it (dry run — nothing is pushed):

```bash
git push --dry-run origin HEAD:main
```

Expected: `pre-push: direct pushes to 'main' are blocked.` and a non-zero exit.

- Uninstall: `git config --unset core.hooksPath`
- Deliberate bypass (only for the docs-only typo exception in `CLAUDE.md`):
  `ALLOW_PUSH_TO_MAIN=1 git push origin main`
- The hook is local only. It does not stop pushes from the GitHub web UI or from
  another clone where it is not installed.

### 2. GitHub labels (repository settings, once)

Already created on the GitHub repository; these commands recreate them (e.g. on a
fork). Issue forms apply `type:*` labels automatically. Every issue gets one `type:*`,
one `priority:*` and at least one `area:*` label.

```bash
gh label create "type:bug"      --color d73a4a --description "Incorrect behavior"
gh label create "type:feature"  --color a2eeef --description "New behavior"
gh label create "type:refactor" --color c5def5 --description "Restructure, no behavior change"
gh label create "type:debt"     --color fbca04 --description "Technical debt"
gh label create "type:docs"     --color 0075ca --description "Documentation"
gh label create "type:chore"    --color ededed --description "Tooling / maintenance"
gh label create "type:ci"       --color 5319e7 --description "CI / quality gates"
gh label create "type:security" --color b60205 --description "Security finding or hardening"
gh label create "priority:P1" --color b60205 --description "Do next"
gh label create "priority:P2" --color d93f0b --description "Soon"
gh label create "priority:P3" --color fef2c0 --description "Eventually"
for a in collectors discovery pipeline requirements scoring ai notifications discord-admin \
         api storage scheduler frontend tests ci config tooling security; do
  gh label create "area:$a" --color bfdadc
done
```

### 3. GitHub merge settings and repository protections (repository settings, once)

Intended merge settings — applied on the private development repository; this public
repository still has GitHub's defaults (merge commits and rebase merging allowed, head
branches kept after merging) until someone with admin rights runs the command below:

- Squash merging: **on**, commit title = pull request title
- Merge commits: **off** (keeps `main` linear)
- Rebase merging: **off** (enable temporarily only for a deliberately atomic
  multi-commit PR)
- Automatically delete head branches: **on**

```bash
gh api -X PATCH repos/{owner}/{repo} \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=COMMIT_MESSAGES
```

**Repository protections on this public repository** (Settings → Rules, Code
security, Actions):

- Ruleset *Protect main* on `main`, no bypass: changes arrive through a pull request
  (no approval required, so a sole maintainer can merge); the checks `test`,
  `frontend` and `gitleaks` from GitHub Actions must pass (see [CI](#ci)); force
  pushes and branch deletion are blocked.
- Secret scanning with push protection, Dependabot alerts and private vulnerability
  reporting (the channel [`SECURITY.md`](../SECURITY.md) points to) are on.
- Actions may use GitHub-owned actions only, need approval for first-time
  contributors' pull requests, and get a read-only `GITHUB_TOKEN`.

The private development repository runs on GitHub's Free plan, where branch
protection, rulesets and secret scanning are not available for private repositories
(its API returns 403 / "disabled"). There, the local pre-push hook and the CI secret
scan stand in for them.

### 4. Personal configuration

`config/profile.yaml` and `config/notifications.yaml` are tracked defaults. Your own
values go in the gitignored `config/profile.local.yaml` and
`config/notifications.local.yaml`, which replace the default file as a whole when present
(`src/config_files.py`). The web UI's profile save and Discord reconciliation write the
`.local.yaml` files; the tracked defaults are only read.

Migrating a clone that has personal edits in the tracked files (nothing is lost — the
app already seeds the local file from the values in effect on its first save, so this
only matters if you edit by hand):

```bash
cp config/profile.yaml config/profile.local.yaml
cp config/notifications.yaml config/notifications.local.yaml
git restore config/profile.yaml config/notifications.yaml
```

Once a `.local.yaml` file exists, edits to the tracked file no longer apply locally.

## Day-to-day flow

```bash
# 1. Start from an up-to-date main
git switch main && git pull --ff-only

# 2. One branch per issue
git switch -c fix/42-rust-title-false-negative

# 3. Work in small steps; commit each green, coherent step (explicit paths, never `git add -A`)
git add src/requirements/principal_skill.py tests/test_requirement_benchmark.py
git commit -m "fix(requirements): match Rust titles case-insensitively"

# 4. Push early and open a draft PR linked to the issue
git push -u origin HEAD
gh pr create --draft --fill --body "Closes #42"

# 5. Keep pushing; check CI
gh pr checks --watch

# 6. Self-review the whole diff, then mark ready and squash-merge
gh pr diff
gh pr ready
gh pr merge --squash --delete-branch

# 7. Back to main
git switch main && git pull --ff-only
```

Never stage `data/`, `config/*.local.yaml`, personal edits to `config/profile.yaml` /
`config/notifications.yaml`, `.env`, logs or caches. Stage explicit paths and check `git diff --cached --stat`
before every commit.

## Local checks

Run the smallest relevant set while working; CI runs the full backend suite.

| Changed area | Targeted tests |
|---|---|
| `src/collectors/` | `pytest tests/test_*collector*.py tests/test_<source>*.py` |
| `src/discovery/` | `pytest tests/test_adaptive_discovery.py tests/test_discovery_query_expansion.py` |
| `src/pipeline/filter.py` | `pytest tests/test_filter_*.py tests/test_hard_filter_*.py` |
| `src/pipeline/scoring.py` | `pytest tests/test_*scoring*.py tests/test_score_*.py tests/test_ranking_*.py tests/test_location_priority_protection.py` |
| `src/requirements/` | `pytest tests/test_education_*.py tests/test_experience_requirement.py tests/test_requirement*.py` |
| `src/ai/` | `pytest tests/test_eligibility_gate*.py tests/test_phase2*.py tests/test_pipeline_ai_integration.py` |
| `src/notifications/` | `pytest tests/test_notification_engine.py tests/test_discord_*.py tests/test_weekly_digest*.py tests/test_system_status*.py` |
| `src/discord_admin/` | `pytest tests/test_discord_admin_*.py` |
| `src/api/` | `pytest tests/test_api_*.py` |
| `src/evaluation/` | `pytest tests/test_evaluation_*.py` |
| `src/storage/` | `pytest tests/test_storage.py tests/test_run_*.py tests/test_source_checkpoints.py` |
| `src/linkedin/`, `src/hellowork/` | `pytest tests/test_linkedin_*.py tests/test_hellowork_*.py` |
| Shared models / orchestrator | full suite: `pytest -m "not network"` (~8 min) |
| `frontend/` | `cd frontend && npm run lint && npm run build && npm test` |

The table is a starting point, not a guarantee — `grep -l <symbol> tests/` finds the
real consumers of what you changed.

Tests marked `network` need live sites and are never required for a PR.

### Evaluation harness (`src/evaluation/`)

Offline benchmark/golden tooling, never imported by production
([ADR 0007](adr/0007-evaluation-harness-outside-production-path.md)):

```bash
python -m src.evaluation validate tests/fixtures/evaluation/datasets/synthetic-ai-filter-50
python -m src.evaluation run tests/fixtures/evaluation/datasets/synthetic-ai-filter-50 --markdown
python -m src.evaluation golden <dataset> <golden.json> --check      # change detector
python -m src.evaluation run <dataset> --assess --markdown          # shadow review metrics
python -m src.evaluation compare <run-a.json> <run-b.json> --dataset <dataset>
python -m src.evaluation self-check --markdown                      # current strategy vs recorded baseline
```

**Self-check** (`src/evaluation/selfcheck.py`) is the repeatable quality check: it replays each
dataset under the current strategy, compares the decisions with the dataset's recorded baseline
(its golden file, so there is one record of "what the system decided before") and writes a report
under `data/benchmarks/self-check/` (gitignored, never overwritten) that the **Lab** page shows.
With no `--dataset` it checks the committed synthetic benchmark against its golden file; add real
labelled datasets with `--dataset <dir>=<golden.json>` (repeatable). It also reports feedback
coverage from `data/jobs.db` (opened read-only; `--db <path>` for another, `--no-feedback` to skip):
how many decided jobs have an explicit answer and where an answer disagrees with the decision. These
are user signals, shown apart and never counted as labels or in the status. Status, worst first: `safety_violation` (a suitable-labelled job
rejected, or a rejection without a recorded reason), `baseline_mismatch` (the baseline was recorded
on other data), `regression` (a job the baseline decided correctly is now wrong), `changed`
(decisions differ without breaking a labelled job, or no baseline is recorded), `pass`. The exit code
is 0 only for `pass`. The report keeps eligibility safety apart from ranking (not measured: the
datasets carry eligibility labels, not relevance labels), groups the current errors into patterns
and points those with a configuration lever at the lab agent (`python -m src.lab agent`), which tests
a bounded candidate in isolation and never approves. Recommendations are tagged `action` (follows from
a measured result) or `suggestion` (to test, not a result). Feedback is reported as signals: latest
explicit answer per job, `unsure` apart, and contradictions with the decision journal as leads to
review — never as labels.

Decisions include the deterministic `score` and per-factor breakdown for accepted jobs (ESCO
synonyms are off unless `--esco-db <path>` is given, so results do not depend on your local
`data/esco.db`). Run results are immutable JSON files under `data/benchmarks/runs/` (gitignored);
`--no-write` only skips that file (the run still reads the dataset and `config/`).
The report also lists pipeline rejections **by reason** (split by human label) and a supplementary
**diagnostic view** that leaves out the jobs rejected for `published_age`. That view is a different
population and is not comparable with the overall metrics: the hard filter stops at its first failing
check, so those jobs' remaining checks were never evaluated.
`golden --check` failing means "a decision changed — review it", not "the system got
worse"; after an intentional change review the diff, then run `golden --update`.
Committed synthetic datasets are regenerated from the Python fixtures with
`python scripts/materialize_evaluation_fixtures.py`. A dataset exported from the real
database lives under `data/benchmarks/` and is never committed.

Real-job dataset workflow (read-only on `jobs.db`, anonymised by default, output must be
git-ignored):

```bash
python -m src.evaluation export --out data/benchmarks/real-v1 --n 200      # → jobs.jsonl + annotations.csv
# fill the `expected` column (accept | reject | borderline) in annotations.csv by hand
python -m src.evaluation annotate-validate data/benchmarks/real-v1 data/benchmarks/real-v1/annotations.csv
python -m src.evaluation annotate-import  data/benchmarks/real-v1 data/benchmarks/real-v1/annotations.csv --labeler <you>
python -m src.evaluation run data/benchmarks/real-v1 --config-dir config --profile config/profile.yaml
```

`annotate-import` stamps provenance (human, labeler, timestamp), merges with existing labels
(re-importing the same file is a no-op) and writes a **new dataset version**, archiving the
previous one under `versions/<n>/`; results always pin the labels hash they were computed
against. `PRIVATE_export_meta.json` maps anonymised ids back to real jobs and holds the
anonymisation salt: never share it. The template is
blind (no production score is shown, to avoid anchoring). `jobs.db` only holds jobs
accepted at the time, so such a dataset exposes false positives but **cannot** measure false
negatives — unless `export --rejected N` is given: it adds up to N jobs whose latest decision-journal
record is a rejection at an opportunity stage (filter, requirements gate, LLM gate, score, ranking;
not validation/dedup), drawn uniformly with the same seed and rebuilt from their journal snapshot,
through the same anonymisation and leak check. False negatives are then measurable on that
subsample only (rejections older than the journal, or whose text retention purged, are not
represented); the stratum and stage stay in the private sidecar, the sheet stays blind, and
`audit --db` replays the same draw. Duplicates are not missed opportunities: a rejected copy whose
deduplication key (`content_hash`) matches a stored job is skipped, and copies of one listing count once.
Stored jobs carry `first_seen_at`, so replays age undated listings as production does (ADR 0012).

### Assisted annotation and label provenance

`python -m src.evaluation.annotation_assist` keeps machine proposals (`assist/proposals.jsonl`) apart
from the reviewer's decisions (`assist/decisions.jsonl`); `apply` only fills empty sheet rows.
`annotate-validate` / `annotate-import` stamp every label of a job with an assistant proposal on record
`label_source=human_assisted`; only a **blind** decision (`show --blind` + `record --blind`: the
proposal was never displayed) stays `human`. `agreement` measures the assistant against blind labels
only. Runs, `audit`, `compare --dataset` and lab experiments report labels by provenance and warn when
results rest on non-independent labels.

Labels imported before this existed are all recorded `human`. `audit` reports them
(`assisted_provenance` WARN) and `run` warns. The correction is a normal re-import of the unchanged
sheet: it only re-stamps provenance (never label content), writes a new version and archives the
current one under `versions/<n>/`, so earlier results stay reproducible:

```bash
python -m src.evaluation annotate-validate data/benchmarks/real-v1 data/benchmarks/real-v1/annotations.csv  # "N assistant-assisted"
python -m src.evaluation annotate-import   data/benchmarks/real-v1 data/benchmarks/real-v1/annotations.csv --labeler <the original labeler>
python -m src.evaluation audit data/benchmarks/real-v1        # provenance + assisted_provenance PASS
```
Expected output of the import: `"changed"` = the number of assisted labels, `"provenance"` split into
`human` / `human_assisted`; label content, labeler and timestamps unchanged. Do it on a copy first if in doubt.

### Collecting independent evidence

Every run report opens with **Evidence behind these numbers**: coverage, accept/reject label counts
with how many are independent, the label basis (`independent`, `mixed`, `not_independent`) and the
sampling limit. When the basis is not `independent` it says **NOT independent validation** before any
metric. A label recorded `human` while an assistant proposal is on record counts as assisted everywhere
(report, `compare --dataset`, which also gives `independent` fixed/broken and McNemar p, and lab
verdicts, which name the provenance of their discordant pairs). Approval stays a human decision.

`real-v1@2` is kept as is (an immutable historical version): all of its 10 accept labels are
assistant-assisted, so its recall and unsafe-rejection rate are agreement with those labels, not measured
quality, and it has no pipeline-rejected sample, so false negatives outside the accepted pool are not
measurable. Independent evidence needs a **new** export labelled without any proposal shown:

```bash
python -m src.evaluation export --db data/jobs.db --out data/benchmarks/real-v2 --n 200 --rejected 100
# label annotations.csv by hand with NO `annotation_assist add-proposals` for this dataset
# (or, if proposals exist: `annotation_assist --dataset data/benchmarks/real-v2 show <batch> --blind`
#  then `record <batch> --blind`; any non-blind `show` makes that batch's labels human_assisted)
python -m src.evaluation annotate-import data/benchmarks/real-v2 data/benchmarks/real-v2/annotations.csv --labeler <you>
python -m src.evaluation audit data/benchmarks/real-v2 --db data/jobs.db
python -m src.evaluation run data/benchmarks/real-v2 --config-dir config --profile config/profile.yaml
```

Unsafe rejections are only measurable on the `--rejected` subsample; the report's sampling line says so.
Below 150 decisive labels every metric stays `insufficient_evidence`, and fewer than 30 accept labels
is flagged as very uncertain. Freshness (`posted_within_days`) is a product decision (ADR 0012): do not
tune it on these metrics.


### Auditing a real export

```bash
python -m src.evaluation audit data/benchmarks/real-v1 --db data/jobs.db
```
Prints PASS/FAIL per check (hash integrity, residual e-mail/URL/phone, `annotations.csv` format,
label provenance, git-ignore status incl. `versions/`, reproducibility of the re-export, and whether the
150-label minimum is met — the last is a `WARN`, not a failure) — counts only, never job content;
exit code 1 on any FAIL. Pass `--db` to also check that no real company name survives in the text (`company_names`; whole-word,
accent/case-insensitive, the same definition the scrubber uses — it is a `WARN` without `--db`) and `--explain` to list each residual identifier as job id / field /
kind / content-free shape. The exporter itself now scans its output with the same detector and
refuses to write a dataset that leaks; every string field is scrubbed, not a fixed list. Run it after `export` and after `annotate-import`.

### Datasets from feedback (P4)

```bash
python -m src.evaluation feedback-dataset --db data/jobs.db --out data/benchmarks/feedback-v1
python -m src.evaluation label-queue --db data/jobs.db --limit 50   # review-flagged jobs awaiting your verdict
```

Labels are *derived* (explicit feedback > outcomes; behaviour never labels; an unmapped legacy
status never labels; a contradicted verdict becomes `borderline`) with reliability in each reason.
Below 100 decisive labels or 20 per class the report says `insufficient_evidence`. Only jobs shown
to the user receive feedback (selection bias, written in the manifest). Read-only on `jobs.db`.

Each job is rebuilt from the snapshot of the decision its label answers; that decision (id, run, outcome,
stage, strategy) is kept in the private sidecar only, so the dataset stays blind. Jobs carry `first_seen_at`
from `jobs` when stored before `--as-of`, as `export` does, so replays apply ADR 0012; jobs never stored carry
none, as in production. Feedback and decisions after `--as-of` are ignored and listings sharing a
`content_hash` count once, so `--salt` + `--as-of` rebuild the same dataset while the journal rows remain
(`audit --db` checks this with the right builder). Datasets built before `first_seen_at` existed are rebuilt
with `--no-first-seen`; their historical versions are never rewritten.

## CI

Workflows run on every pull request and on pushes to `main`:

| Workflow | What it runs | Gate |
|---|---|---|
| [`tests.yml`](../.github/workflows/tests.yml) | `pytest -m "not network"` (8–15 min), then `mypy src` in the same job | tests blocking (required check `test`), mypy advisory |
| [`frontend.yml`](../.github/workflows/frontend.yml) | `npm ci`, oxlint, `tsc -b`, `vite build`, Vitest, `npm audit` (runtime deps) — every time, with no path filter (about a minute) | blocking (required check `frontend`) |
| [`lint.yml`](../.github/workflows/lint.yml) | `ruff check src tests` — only when Python sources change | advisory, not required |
| [`secret-scan.yml`](../.github/workflows/secret-scan.yml) | Gitleaks CLI over the commits the PR/push adds, output redacted | blocking (required check `gitleaks`) |

A required check must report on every pull request: GitHub leaves the check of a
path-filtered workflow that did not run *pending*, which blocks the merge. So the
three required workflows have no path filter; only the advisory Lint keeps one. If a
workflow is ever made required, remove its path filter first.

Cost and safety rules the workflows follow: a newer push to a pull request cancels
its running checks (`concurrency`); jobs have timeouts; third-party actions are
pinned to a commit SHA (with the tag in a comment — update both together); every
workflow has read-only `contents` permission, uses `pull_request` (never
`pull_request_target`), so pull requests from forks get no secrets and no write token.

### Run the CI checks locally

The same commands, from the repository root with the virtual environment active:

```bash
pip install -e ".[dev,mcp_admin]"
pytest -m "not network"          # Tests (stop a local backend first: see Troubleshooting)
mypy src                         # Tests, advisory step — baseline docs/baselines/mypy.md
ruff check src tests             # Lint, advisory — baseline docs/baselines/ruff.md
cd frontend && npm ci && npm run lint && npx tsc -b && npx vite build && npm test && npm audit --omit=dev
```

The secret scan needs the Gitleaks CLI (`gitleaks git --config .gitleaks.toml --redact`).

If GitHub refuses to start jobs with *"recent account payments have failed or your
spending limit needs to be increased"*, nothing in the workflows is wrong: the
account's billing blocks every job (0 steps run). Run the commands above locally and
record the results in the pull request until the account owner resolves it.

If the secret scan fails: **revoke or rotate the secret first** (a pushed secret is
compromised even if the commit is later removed), then remove it from the branch with
a new commit. Do not rewrite published history.

- A PR is mergeable only when CI is green.
- CI failures are fixed with new commits on the PR branch — never on `main`.
- If `main` itself goes red, fix it through a `fix/` PR immediately (or revert the
  offending commit via PR).

## Troubleshooting

- **Tests error with "Test wrote to the real data/ directory"** — the autouse guard in
  `tests/conftest.py` snapshots `data/` and `config/`. A running backend or scheduler
  writing `data/jobs.db` during the test run trips it. Stop the local backend before
  running the full suite.
- **A test needs a realistic candidate profile** — load
  `tests.fixtures.profiles.TEST_PROFILE_PATH` (a frozen snapshot), never
  `config/profile.yaml`: personal edits must not change test results.
  `tests/test_no_personal_profile_in_tests.py` fails if a test reads the personal file.
- **Hook does not run** — check `git config --get core.hooksPath` prints `.githooks`.
- **`pre-push: direct pushes to 'main' are blocked`** — expected; push a branch and open
  a PR.

## Agent skills

- `.agents/skills/` — tracked, vendored skill pack shared by all coding agents
  (currently `mattpocock/skills`). `skills-lock.json` pins
  exactly these skills by source and hash; keep the two in sync when adding, updating
  or removing one. Never hand-edit a vendored skill: fix it in its source repository
  and pull the change back with `npx skills update <skill>`.
- `.claude/` — gitignored, per-machine. `.claude/skills/` may symlink into
  `.agents/skills/` and may hold personal, Claude-only skills. Those are not tracked
  and not listed in `skills-lock.json`; a fresh clone does not have them.
- Skills are used on demand; one not referenced in `CLAUDE.md`/`AGENTS.md` is not
  unused and is not removed for that reason.

### Installing or restoring a skill

Always install to `claude-code` **and** `universal` together:

```bash
npx skills add <owner>/<repo> -a claude-code -a universal -s <skill>
```

With a single target directory the CLI silently uses copy mode and skips the canonical
`.agents/skills/` location, leaving the skill only in gitignored `.claude/` while still
writing a `skills-lock.json` entry. Two targets give symlink mode: real content in
`.agents/skills/<skill>`, a junction at `.claude/skills/<skill>`. On Windows the link is
a junction, so no elevation or Developer Mode is needed.

After a fresh clone `.agents/skills/` is present but the `.claude/skills/` links are
not, so no skill is discoverable until they are recreated — run the command above per
skill. `npx skills experimental_install` restores `.agents/skills/` from the lockfile
**only**; it does not create the `.claude/` links.

### Experiment Lab (P5)

```bash
python -m src.lab hypothesis "raise skills weight reduces false accepts"
python -m src.lab candidate <hyp-id> --overrides '{"scoring.yaml": {"scoring": {...}}}'
python -m src.lab run <cand-id> data/benchmarks/feedback-v1      # baseline vs candidate, verdict + guardrails
python -m src.lab approve <exp-id> --dataset data/benchmarks/feedback-v1 --by <you> --reason "..."   # human only, in your own terminal
#   re-runs the experiment (the stored verdict is not trusted), shows the evidence, asks for a typed code;
#   add --accept-exploratory-evidence to approve evidence that is not independently validated (recorded)
python -m src.lab status                                          # latest pointer + what the pipeline will run
python -m src.lab deactivate --by <you> --reason "..."            # human only; back to config/
python -m src.lab rollback <pointer-id> --by <you> --reason "..."
python -m src.lab --lab-db data/lab/lab.db isolation-check        # as the agent's OS account: exit 3 unless isolated
python -m src.lab agent data/benchmarks/real-v1 --profile config/profile.yaml   # investigate → hypotheses → experiments → recommendations
python -m src.lab memory --kind hypothesis --dataset data/benchmarks/real-v1 --profile config/profile.yaml   # status + staleness
# every command above except approve/rollback/deactivate/isolation-check runs under the lab write guard
# (ADR 0011): it can write only data/lab.db and data/lab/strategies, read jobs.db read-only, start no
# process, and reach no network
```
The guard is not a sandbox, and the human commands' typed confirmation is procedural: an automated agent
is kept out of production state only by running it under an OS account that cannot write `jobs.db`,
`config/`, the code, `data/benchmarks/` or credentials (ADR 0011, "Promotion authority"); check that
account with `isolation-check`, and start the agent with `agent --require-isolation` to fail closed.

#### Sandboxed lab agent (Windows AppContainer, no administrator)

The simplest real boundary: run the agent in an AppContainer (`src/lab/sandbox.py`), the Windows
sandbox Edge and Store apps run in. It needs no second account and no elevation. A process inside it
can read only what was granted to the container and write only `data\lab\`. It cannot read `.env*` or
`config\*.local.*`, cannot reach the network (`--internet` allows outbound internet), and the OS
refuses its loopback connections, so neither the API nor a relay is reachable.

**Only a command started with `sandbox run` is inside that boundary.** The same agent started any other
way (a plain `python -m src.lab agent`, a scheduled task, an editor or an LLM tool) runs with all your
rights, and only the in-process guard stands between it and production; `isolation-check` then reports
`"os_enforced": false`.

```powershell
python -m src.lab sandbox grant              # plan: the icacls commands (they name the container only)
python -m src.lab sandbox grant --apply      # apply; re-run after adding top-level files (they start closed)
python -m src.lab sandbox run -- python -m src.lab --lab-db data/lab/lab.db isolation-check   # "os_enforced": true
python -m src.lab sandbox run -- python -m src.lab --lab-db data/lab/lab.db agent tests/fixtures/evaluation/datasets/synthetic-ai-filter-50 --require-isolation
python -m src.lab sandbox revoke --apply     # remove every ACE the grant added
```

- A leading `python` in `run` means the interpreter running the command. `grant` also gives it read
  access to that interpreter's installation (`sys.prefix`, `sys.base_prefix`).
- To confine another tool (an LLM CLI), grant its installation with `--grant-read <folder>` and run it
  with `--internet`. Whatever it needs to write must be under `data\lab\` or in the container's own
  folder (`%LOCALAPPDATA%\Packages\findyourjobwithai.lab\AC`, its `TEMP`/`HOME`).
- The sandboxed command gets system environment variables only: no API keys or tokens.
- `run` refuses to start from an elevated (administrator) window. The human commands
  (`approve`/`rollback`/`deactivate`) refuse to run inside the container.
- Measured on Windows 11 (`tests/test_lab_sandbox.py` starts a real container): writing `jobs.db`,
  `config\`, `data\` or `src\`, reading `.env` (also one created after the grant), loopback to a
  listening port and the internet are all refused. `isolation-check` run inside reports
  `"os_enforced": true`. A deny ACE for the container is ignored by Windows, so secrets are kept
  closed by never being granted.
- It can read production data (`jobs.db`, `config\`), which replays need. It cannot write them.
- `revoke` tries every path even when one fails, then lists the failures; re-running it is safe, and
  it also cleans up after an interrupted `grant`. Pass it the same `--grant-read` folders as the grant,
  or their ACEs stay. Uninstalling does not revoke: run `revoke --apply` first, or the kept `data\` and
  `config\` keep ACEs naming the container (harmless while nothing runs in it).
- Links do not widen the grant: inside the container, creating a junction or a hard link to a file it
  cannot write is refused, and symlinks need a privilege it lacks (measured).

#### Isolated lab agent (separate account, manual OS setup)

Isolation is only established once `isolation-check` **run by you, in a window of the agent's account**,
prints `"os_enforced": true`. Until then "only a human promotes" is procedural. Four things must hold,
and that check probes all four from the agent's account:

1. **Files**: NTFS denies the account every write, delete, rename, ACL and owner change in the
   repository except `data\lab\`. It also denies it reading secrets (`.env*` except `.env.example`,
   `config\*.local.*`): `.env` holds e.g. the n8n API key, and n8n can call the API as you.
2. **Places you execute from**: the account must not be able to write, or create, anything you run.
   That covers PATH directories, the programs and scripts your scheduled tasks start, Startup items and
   Run keys. Code planted there runs **as you**, which defeats everything else.
   - A secondary drive often grants *Authenticated Users* Full Control at its root. Without step 3
     below, an agent account could then replace a tool installed there that is on your PATH (`git.exe`).
   - It could also replace a script that one of your scheduled tasks runs as you.
   - And it could create a missing program that a scheduled task still points at, under a folder any
     account may create.
   - The same applies to links: a junction (e.g. under `.claude\skills\`) whose target is missing can be
     created by any account, which could plant skills that **your** Claude Code loads.
3. **API**: the API runs as you and writes production state for whoever it serves, so it must refuse the
   account. Start it with `API_SAME_USER_ONLY=1` (environment or `.env`).
   - It then asks Windows which process owns each loopback connection and serves only its own
     account (`src/api/peer_guard.py`).
   - It refuses everything else: other accounts, non-loopback peers, connections it cannot attribute
     to one owner, and any request carrying a forwarding header (`X-Forwarded-For`, `Forwarded`, …).
     uvicorn trusts `X-Forwarded-For` from loopback by default and rewrites the client address, port
     included. An agent could otherwise name the port of one of *your* open connections (any account can
     list them) and be served as you. That was reproduced against a real server before the fix.
   - In this mode the Vite origins are also dropped from CORS and the origin guard.
   - It is off by default.
4. **No relays**: a service of *your* account that forwards requests to the API is served as you. That
   includes the Vite dev proxy on 5173 (often on `::1` only), n8n on 5678 (its webhooks and API, via
   Docker's proxy), or anything else. Stop them while the agent runs. The `FindYourJobWithAI_N8n` task
   starts n8n at logon, so disable it for those sessions. The check fails if anything answers on those
   ports, on 127.0.0.1 or ::1.

Setup, as yourself. Every step marked *elevated* changes machine security settings and is yours to
authorize and run:

```powershell
# 1. elevated, once: a STANDARD account (never an administrator); you type its password at the prompt
net user fyj-lab * /add
# 2. not elevated: plan, apply, then audit the repository ACLs (Plan changes nothing; read it first)
powershell -ExecutionPolicy Bypass -File scripts\lab_isolation.ps1 -Repo . -Account fyj-lab
powershell -ExecutionPolicy Bypass -File scripts\lab_isolation.ps1 -Repo . -Account fyj-lab -Mode Apply
# 3. elevated: the same deny on D:\ (only for fyj-lab; nobody else's access changes; it propagates to
#    every file on D: and can take minutes). The explicit allow on data\lab is nearer, so it still wins.
powershell -ExecutionPolicy Bypass -File scripts\lab_isolation.ps1 -Repo . -Account fyj-lab -DenyRoot D:\ -Mode Apply
#    Add every other folder the check reports writable, separated by ';' (powershell -File passes
#    "a,b" as one string and refuses a repeated -DenyRoot), e.g. -DenyRoot "D:\;C:\Games\SomeLauncher"
#    (a program folder Users may write, started as you by a Run key). The system drive's root is
#    refused: the account's profile lives there.
# 4. not elevated: audit; it prints the exact isolation-check command for step 6
powershell -ExecutionPolicy Bypass -File scripts\lab_isolation.ps1 -Repo . -Account fyj-lab -DenyRoot D:\ -Mode Audit
# rollback at any time: the account's explicit ACEs only (elevated if -DenyRoot D:\ was applied)
powershell -ExecutionPolicy Bypass -File scripts\lab_isolation.ps1 -Repo . -Account fyj-lab -DenyRoot D:\ -Mode Remove
```

- **Apply** is idempotent: ACEs already present are skipped.
- **The script refuses** an `-Account` that is you, a group you belong to, or a built-in principal (Users, Authenticated Users, Everyone, …), in every mode: a deny for them would lock you out too (with `-DenyRoot D:\`, of the whole drive).
- **Remove** deletes only explicit ACEs whose principal is `-Account`, on the paths Apply touches; other principals' ACEs and inherited ones are left as they are.
- On a failure, Apply stops, names the step and exits 2. Steps already done stay; re-run, or Remove.
- **What Apply does:**
  - creates `data\lab`;
  - grants the account Modify there;
  - denies it `ReadData` on each secret file that exists **now** (re-run Apply after adding one);
  - denies it, inherited by everything below each root, the exact mask `0x000D0156`. That covers
    write data, append, write attributes and extended attributes, delete, delete child, change
    permissions and take ownership. Reads stay allowed.
- **Do not use `icacls /deny` for this.** icacls adds `SYNCHRONIZE` to every deny ACE (verified:
  `/deny (WD,AD,WEA,WA,D,DC,WDAC,WO)` is stored as `0x001D0156`). Denying `SYNCHRONIZE` also blocks
  opening files for reading, so the agent could no longer read `src\` or `config\`.
- **What Audit checks**, beyond the ACEs above (it exits 1 on any of them):
  - an object the account owns, which keeps an implicit `WRITE_DAC` whatever the deny says;
  - inheritance disabled on an object the account or one of its groups may write (the deny does not
    reach it; pytest's owner-only `.pytest_cache` is not one);
  - an explicit Allow on a child for the account or one of its groups, which is evaluated before the
    inherited deny;
  - links leading outside the repository;
  - membership of Administrators.
- **What Audit lists:** every other principal that may write the repository (e.g. a coding
  agent's sandbox group with Modify). These do not weaken `fyj-lab`'s boundary: the explicit deny is ordered before their
  allow. But each of them can change production in its own right.
- Audit also lists what runs as you or another account: PATH, scheduled tasks of every principal but
  the agent's, service binaries, every file in the Startup folders, Run and RunOnce keys (64- and
  32-bit), git's `core.hooksPath` and helpers, and the commands of Claude Code / Codex hooks and MCP
  servers. Often a few hundred, too many for a command line: it writes them to
  `data\isolation-exec-paths.txt` and the check reads them with `--exec-paths-file`.
- A missing program counts as plantable when the account can create the folders leading to it: drive
  roots usually let every user create folders, and the creator owns what it creates.

Machine-level remediation, beyond steps 1–4 (all yours to decide):

- **Dangling links** (e.g. `.claude\skills\*` junctions): re-point them at `.agents\skills\*` in this
  repository, or remove them. `.claude\` is yours, so the scripts never touch it.
- **Scheduled tasks that start a missing program** under a folder any account may write: delete them,
  or recreate them pointing at a protected location.
- **A drive root with Full Control for Authenticated Users** is a machine-wide setting. Step 3
  neutralises it for `fyj-lab` only. Reducing it for everyone, e.g. to the Windows defaults (Users: read
  & execute, create folders), is a separate decision: it also affects other sandbox accounts.

The agent account needs its own Python. Yours, and `.venv`'s base interpreter, live under your profile,
which other accounts cannot read:

- Install Python for all users, or in the agent's profile.
- Create a virtual environment **outside** the repository, e.g. `C:\Users\fyj-lab\fyj-venv`, and install
  the dependencies with `pip install <repo>` or `PYTHONPATH=<repo>` (`pip install -e` would write to the
  repository).
- Set `PYTHONDONTWRITEBYTECODE=1`, since the repository is not writable.

Verify (step 6), with the API running under `API_SAME_USER_ONLY=1` and no relay:

```bash
runas /user:fyj-lab cmd        # then, in that window, from the repository, paste the command Audit printed:
python -m src.lab --lab-db data/lab/lab.db isolation-check --exec-paths-file data/isolation-exec-paths.txt
python -m src.lab --lab-db data/lab/lab.db agent <dataset> --require-isolation   # same check, fails closed
```

- **Exit status:** `isolation-check` exits 3 unless every one of these holds:
  - the account can write no protected target and read no secret;
  - it can write or create none of the `--exec-path` places, and no link target outside the repository;
  - it is not in Administrators;
  - port 8000 answers with the API guard's refusal;
  - 5173 and 5678 answer on neither loopback address.
- **Changing the ports:** `--api-port` and `--relay-port` override 8000 and 5173/5678.
- **An API that is not listening** counts as **unverified**, not as safe. It may start later without the
  guard, and while the port is free the account could bind it itself. It can also bind `[::1]:8000`
  while the API holds `127.0.0.1:8000`. Open the UI at `http://127.0.0.1:8000`, not `localhost`. A page
  served on `[::1]` cannot write through the real API (the origin guard refuses its `Origin`), but it
  could imitate the UI.
- **Lab database location:** `agent --require-isolation` insists on a lab database under `data\lab\`.
  That is where the Lab page and `GET /api/lab/*` read it when `data\lab.db` is absent; move an old
  `data\lab.db` there yourself.

What the setup proves, and what it does not:

- **Enforced by Windows:**
  - Every process the agent starts (a shell, `python` directly, another tool) runs with the account's
    token, so the NTFS denies apply to all of them. Verified with a stand-in: a direct read-write
    `sqlite3` connection to a live WAL `jobs.db` is refused, while a read-only connection still reads it.
  - The API's refusal is decided from the kernel's record of which process owns the connection, which
    the account cannot forge once forwarding headers are refused.
- **Enforced in code (whatever the account):**
  - Approval recomputes its evidence and never trusts `lab.db`.
  - Bundles are content-addressed and re-verified by production at every run.
  - The guarded lab commands start no process and open no network connection.
  - `approve` opens the agent-writable `lab.db` read-write, but only to locate the experiment and to
    append the approval record. Triggers planted there cannot reach `jobs.db` (separate connection, no
    `ATTACH`).
- **Procedural:**
  - creating the account, never giving it your password (no `runas /savecred`), never adding it to
    Administrators;
  - starting the API with the flag, and keeping relays stopped while the agent runs;
  - re-running Audit after ACL changes, new secrets or new tools on your PATH;
  - running `approve`/`rollback`/`deactivate` only from your own account and terminal;
  - running the check yourself: an agent can misreport a check it ran.
- **Remaining risks:**
  - The account can read `jobs.db` (personal data; the lab reads it by design) and anything not listed
    as a secret.
  - Places outside `D:\` and the listed exec locations are covered only as far as Audit enumerates
    them. For example, a program that loads plugins from a folder any account may write (on `C:\`,
    `C:\ProgramData` and the root accept new folders from any user) is not found.
  - Content it writes in `data\lab\` is read by your API and by `approve` as data: recomputed or
    hash-checked, never executed. It is still **untrusted text**: an LLM agent of yours (Claude Code,
    Codex) asked to read the agent's hypotheses or reports runs with your rights, so instructions planted
    there are a prompt-injection path back to production. `CLAUDE.md` §8 makes
    lab output untrusted data for every agent working in this repository; a session started elsewhere
    has no such rule, so do not point one at lab content.
  - Port squatting while the API is down (above).

Evidence before calling the setup complete:

- Audit exits 0: no `PROBLEM` line, and each `NOTE` reviewed.
- The check you ran as the account prints `"os_enforced": true` with `"8000": "refused"`.
- `whoami /groups` as the account shows no Administrators.
- As the account, `Invoke-WebRequest http://127.0.0.1:8000/api/lab/active-strategy` fails with 403,
  and so does the same request with `-Headers @{'X-Forwarded-For'='127.0.0.1:1'}`.

See ADR 0011. `run_pipeline` honours the latest pointer: an approved bundle replaces `config/filters.yaml`
and `config/scoring.yaml`; a missing/modified bundle, a pointer approved before pointers were honoured
(`inert`), or a bundle that no longer reproduces from the current `config/` (`stale`, e.g. after you edit
`config/filters.yaml`) all fall back to `config/`. The run's `stats.active_strategy` and the Lab page
say which one ran. The lab commands replay that same strategy by default (`--base-config active`).
