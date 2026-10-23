# Public release readiness

Assessment of the first public release (2026-10-18). The project was developed in a
private repository; this public repository starts from one reviewed snapshot of it. The
private history is not published (see *History* below). Secrets are never reproduced
here.

> Historical record. Discord, n8n, the weekly digest and `config/notifications.yaml`
> described below were removed after 1.0 (2.0 plan F1.1–F1.3).

**Verdict:** suitable for public release with the limitations listed in §6. No live
credential exists in this repository. The application binds to `127.0.0.1`, sends no
telemetry of its own and uses no cloud AI.

## 1. What this release contains

The final application code, with five sets of changes made for the public release:

| Area | Change |
|---|---|
| Privacy | the dashboard's Discord link comes from `DISCORD_GUILD_ID` (no hard-coded server); the n8n digest recipient comes from an n8n variable; `scripts/Start_n8n.bat` binds n8n to `127.0.0.1` with telemetry and update checks off; Hugging Face Hub telemetry is turned off; frontend dependencies with advisories updated |
| Configuration | `config/profile.yaml` and `config/notifications.yaml` are neutral examples. Personal values go in the git-ignored `config/*.local.yaml` overrides, which the Profile page and the Discord reconciler write. Evaluation replays of datasets that pin no profile use the profile in effect |
| CI | actions pinned to commit SHAs, `contents: read` only, superseded runs cancelled, job timeouts, CPU-only torch, `npm ci --ignore-scripts`, `npm audit` |
| Windows launcher | one entry point, `FindYourJob.cmd` (see [windows-app.md](windows-app.md)); `GET /api/health`; unknown `/api/*` paths return 404 |
| Documentation | README, [reference](reference.md), [SECURITY.md](../SECURITY.md), [CONTRIBUTING.md](../CONTRIBUTING.md), third-party notice for the vendored agent skills, and this report |
| Release verification fixes | found by running CI's commands on Linux and the launcher live: a stop requested while the backend starts now exits with code 0 instead of a silent failure; `mypy src` is clean on Linux as well as Windows; the Gitleaks step takes its commit range from `env`; `scripts/install_startup.py` names `Start_backend.bat` with its real case; the dashboard tab is titled "Find Your Job With AI"; the n8n digest workflow no longer carries the exporting n8n instance's ID; the README explains how to turn off each background scanner |

The architecture, ADRs, matching safeguards and the separation between production and
the experiment lab are unchanged.

## 2. History

Only the reviewed tree is published, as a single baseline commit. The private
history contained personal data that has no place in a public repository: an earlier
personal profile and notification settings, a personal e-mail address (in a workflow
file and as commit author), Discord server and channel IDs, local file-system paths,
and a Discord webhook URL that was revoked long before this release. None of it is in
this tree.

Issue and pull-request numbers in code comments and documentation (for example
"issue #9") refer to the private tracker. They do not link to issues here.

## 3. Audit of the published tree

- **Secrets and personal data:** every file in the tree was scanned for webhooks and bot
  tokens, cloud and API tokens, private keys, JWTs, credentials in URLs, secret
  assignments, e-mail addresses, phone numbers, Discord IDs and user paths. Findings are
  intentional synthetic fixtures (redaction tests, fake webhook URLs and IDs in tests).
  The Gitleaks CLI (v8.30.1, release archive checked against its published SHA-256)
  with `.gitleaks.toml` finds no leak in the commit or in the files. Five synthetic
  values that its default rules match (a guild ID, a job key, three import lines)
  carry the inline `gitleaks:allow` marker.
- **Indeed app key:** the Indeed collector needs the API key of Indeed's iOS app. It is
  Indeed's credential, not this project's, so it is not in this repository: set
  `INDEED_API_KEY` in `.env` (see `.env.example`). Without it the collector makes no
  request and reports one error. The project cannot rotate or revoke that key; only
  Indeed can.
- **Machine details:** the lab isolation guide (`docs/development.md`, ADR 0011)
  describes the risks generically; it names no real machine's programs, accounts or
  permissions.
- **Generated artifacts:** no databases, logs, caches, model files, build output or
  bytecode are tracked.
- **Test fixtures:** `tests/fixtures/**/profile.yaml` are frozen test profiles (target
  domains, French departments, junior level) that the scoring tests depend on; they
  contain no name or contact details.

## 4. Dependencies and licences

| Item | Assessment |
|---|---|
| Project licence | MIT ([LICENSE](../LICENSE)) |
| Frontend runtime dependencies | all MIT |
| Python runtime dependencies | mostly MIT, BSD or Apache-2.0, plus MPL-2.0 (`certifi`, part of `tqdm`), PSF-2.0 and ISC — all installed unmodified from PyPI, none vendored; optional `python-jobspy` (MIT), `crawl4ai` (Apache-2.0), `mcp` (MIT). The embedding model (`paraphrase-multilingual-MiniLM-L12-v2`, Apache-2.0) is downloaded on first use, not bundled |
| Frontend | `npm audit`: 0 vulnerabilities (runtime and development) |
| Python advisories (`pip-audit`, fresh install) | 0 known vulnerabilities in the 94 packages a fresh `pip install -e ".[dev,mcp_admin,linkedin]"` resolves (2026-10-18: transformers 5.20.1, sentence-transformers 6.1.0, python-jobspy 1.3.0, markdownify 1.2.3, …). Versions are ranges, not pins |
| Older environments | an environment installed earlier can still hold `transformers` ≤ 5.9 (three advisories: LightGlue model loading, custom generation code, `save_pretrained` paths — none reachable here: nothing imports `transformers` directly or calls `generate`/`save_pretrained`, and the one model loads with `trust_remote_code=False`) or `markdownify` < 0.14.1 (one low advisory). `pip install -U -e .` updates them |
| Vendored agent skills (`.agents/skills/`) | 25 skills from `mattpocock/skills` (MIT), with the required notice in `.agents/skills/THIRD_PARTY_NOTICES.md`; each of the 74 files is byte-identical (line endings aside) to the file at a commit of the upstream repository. Development tooling only; the application does not use them |
| `frontend/public/favicon.svg`, `icons.svg` | scaffold files of Vite's `create-vite` React template (MIT), which is meant to be copied: the favicon is the Vite logo; `icons.svg` (template social icons) is unused |

## 5. Security properties

See [SECURITY.md](../SECURITY.md) for the threat model. In short: the backend listens on
`127.0.0.1` only, refuses foreign `Host` headers (DNS rebinding) and cross-site writes,
and can be limited to the current Windows account (`API_SAME_USER_ONLY=1`). It has no
login: any program running on the same computer can use it. Subprocesses use fixed
argument lists; there is no `shell=True`, `eval`, `pickle` or unsafe YAML in `src/`. The
lab API is read-only and promotions need explicit human approval (ADR 0011).

The README's *Privacy and network behaviour* section lists every outbound connection.
Collecting jobs needs the internet; with the embedding model cached and
`HF_HUB_OFFLINE=1`, everything that works on stored data runs offline.

## 6. Limitations

- **n8n started outside the project.** `scripts/Start_n8n.bat` and
  `FindYourJob.cmd --n8n` start n8n on `127.0.0.1` with telemetry off. `n8n start`,
  `npx n8n` or Docker use n8n's own defaults: all network interfaces, telemetry on.
  n8n also contacts n8n.io when you browse its templates.
- **Lab agent isolation.** OS-level isolation of an autonomous lab agent depends on
  manual Windows account and permission setup (`docs/development.md`); it is not
  automated and has not been proven. Treat lab output as untrusted.
- **Launcher.** Tested live on Windows 11 from a fresh clone: cold start with no
  database and no dashboard build (it built the dashboard, created `data/jobs.db` and
  `data/esco.db`, served the dashboard), `status`, `stop`, `--n8n` reusing an n8n that
  was already running (left running afterwards), `dev` (Vite on `127.0.0.1:5173`
  proxying the API), and `shortcut` create/remove. Not tested live: the Edge/Chrome app
  window, and `--n8n` starting n8n itself.
- **CI on GitHub.** The workflows have not run on GitHub-hosted runners: Actions on the
  maintainer's account refuses to start jobs ("recent account payments have failed or
  your spending limit needs to be increased"). Each workflow's commands were run
  instead on Windows and, for the runner's OS, in Linux containers (`python:3.12` as a
  non-root user, `node:24`) with the same pinned tool versions; results in §7. That is
  local evidence of what CI would do, not a GitHub result: no hosted workflow run has
  passed yet. The publication procedure requires Tests, Frontend, Lint and Secret scan
  to pass on GitHub for this exact commit, while the repository is still private,
  before it is made public.
- **Automatic scans.** In the shipped config, LinkedIn and HelloWork scans start live
  shortly after the backend first starts, then daily. The README's *Automatic job scans*
  section says how to limit or turn off each one; every setting there was checked
  against a fresh backend.
- **Region-specific defaults.** The scoring has a fixed PACA-neighbour tier, the jobs
  API's department filter defaults to `83` (Var), and the frozen test profiles prefer
  Var and a few other French departments. This reflects the region the project was
  first built for and is kept as product behaviour; users elsewhere set their own
  departments in the Profile page.
- **Known follow-ups.** Some benchmark scripts under `scripts/` read the tracked example
  profile unless given `--profile`; sentence-transformers 6 warns that
  `get_sentence_embedding_dimension` is renamed (`src/embeddings/embedding.py`); a test
  run leaves a backend log under `logs/backend/` (git-ignored).

## 7. Validation of this tree

Run with temporary databases only, on the exact commit and tree of this release (this
report included), from fresh installs made exactly as the workflows make them: CPU-only torch, then `pip install -e ".[dev,mcp_admin]"` — on 2026-10-18 that
resolved torch 2.14.1, transformers 5.20.1, sentence-transformers 6.1.0, numpy 2.5.4.

| Check | Windows 11, Python 3.12.10 | Linux (`python:3.12`, non-root; `node:24`) |
|---|---|---|
| `pytest -m "not network"` | 5083 passed, 37 skipped, 0 failed | 5064 passed, 56 skipped, 0 failed |
| `ruff check src tests` (Ruff 0.16.2, as `lint.yml`) | 31 findings, the baseline in `docs/baselines/ruff.md` | 31, same |
| `mypy src` (mypy 2.3.0) | no issues in 191 source files | no issues in 191 source files |
| Frontend `npm ci --ignore-scripts`, `npm run lint` | 0 errors, 3 pre-existing warnings | same |
| Frontend `tsc -b`, `vite build` | pass | pass |
| Frontend `vitest run` | 107 passed (17 files) | 107 passed (17 files) |
| `npm audit` (all, and `--omit=dev --audit-level=high`) | 0 vulnerabilities | 0 vulnerabilities |
| `pip-audit` on a fresh resolution with the `linkedin` extra | 0 known vulnerabilities (94 packages) | — |
| Gitleaks CLI 8.30.1 + `.gitleaks.toml` (`git` and `dir` modes) | no leaks | no leaks (run locally with `secret-scan.yml`'s commands, not on GitHub) |
| Secret and personal-data scan of every file | no secrets; only the synthetic fixtures listed in §3 | — |

Skipped tests: those needing pandas (the `linkedin` extra, which CI leaves out on
purpose) and, on Linux, the Windows-only launcher tests.
