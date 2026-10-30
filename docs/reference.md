# Technical reference

The detailed companion to the [README](../README.md): pipeline stages, scoring,
collectors, caches, the web API and the database. Durable decisions
and their reasons are in the [ADRs](adr/README.md); stage internals (decision journal,
feedback journal, evaluation harness, lab) are in [architecture.md](architecture.md).
Source code is the source of truth: when this page and the code disagree, the code wins.

## Pipeline Stages

| # | Stage | Module | Purpose |
|---|---|---|---|
| 1 | Discovery / query expansion | `src/discovery/registry.py`, `src/discovery/query_expansion.py` | Loads `config/sources.yaml`; generates per-source, per-profile multilingual search queries (a hardcoded baseline plus optional LLM-generated additions, capped at 12 per source) |
| 2 | Collection | `src/collectors/*.py` | Fetches raw jobs from each active source; every collector returns a `CollectorResult` and never raises — network/HTTP/timeout errors are caught and logged, never crash the run |
| 3 | Normalization | `src/pipeline/normalize.py` | Maps source-specific payloads to the shared `Job` Pydantic model |
| 4 | Validation | `src/pipeline/validate.py` | Rejects jobs missing required fields or with invalid data |
| 5 | Deduplication | `src/pipeline/deduplicate.py` | Intra- and cross-source identity resolution (see below) |
| 6 | Hard filtering | `src/pipeline/filter.py` | Location, remote policy, work mode, seniority (title keywords; off for a v2 profile, where the requirements gate weighs the career stage, ADR 0004), publication age, salary, title-keyword exclusions, contract type — first failing check rejects the job |
| 7 | Requirements gate | `src/requirements/gate.py` (`HardConstraintGate`) | Hard/soft rejection based on stated education, years of experience, and principal/mandatory skills vs. your profile (see [Job Scoring](#job-scoring)) |
| 8 | LLM eligibility gate *(opt-in)* | `src/ai/eligibility.py` (`EligibilityGate`) | Optional local-LLM check for *implicit* mismatches the deterministic gate can't see (e.g. a title that conventionally implies a degree level without stating it). Fail-open; no-op unless enabled |
| 9 | Deterministic scoring | `src/pipeline/scoring.py` | 5-factor weighted score with domain/seniority hard gates and a tier classification |
| 10 | Storage | `src/storage/sqlite.py` | Batch upsert into `jobs.db`, including the FTS5 full-text index |
| 11 | Local AI enrichment *(opt-in, `--ai`)* | `src/ai/pipeline.py` (`Phase2Pipeline`) | Runs a local LLM over the top-ranked candidates only, to enrich the text semantic matching uses; failures are logged and never block the run |
| 12 | Semantic matching | `src/pipeline/semantic_matching.py` | Cosine similarity between job and profile embeddings, blended into the final score |
| 13 | Personalization | `src/personalization/engine.py` | Bounded, feedback-driven score adjustment; re-applies the scoring gates afterward so positive feedback can't undo a hard gate |
| — | Output | Web dashboard, CLI summary | See the dedicated sections below |

### Deduplication

Two passes, designed to avoid merging legitimate distinct postings:

**Intra-source** (same `source` value):
1. `(source, source_job_id)` — definite duplicate.
2. `(source, canonical_url)` — definite duplicate when `source_job_id` is absent.
3. Neither available — keep unconditionally; never merge on title/company alone.

**Cross-source** (different `source` values):
1. Normalized `canonical_url` — a global identifier.
2. `(company_norm, title_norm, location_norm, employment_type)` — content identity,
   **only when company is non-empty**. Anonymous/unknown-employer jobs are never merged
   on content alone.

The same title at the same company in a different city, or under a different contract
type, is correctly kept as a distinct posting.

## Job Scoring

`src/pipeline/scoring.py` computes a weighted composite score, then applies hard gates
and soft penalties on top.

### Weighted factors (`config/scoring.yaml`)

| Factor | Default weight | Priority |
|---|---|---|
| `location_fit` | 0.30 | Highest |
| `domain_relevance` | 0.25 | |
| `skills_match` | 0.20 | |
| `seniority_match` | 0.15 | |
| `experience_fit` | 0.10 | Lowest |

### Hard gates (applied to the weighted composite)

- **Domain gate** — if `domain_relevance` is at or below the "confirmed wrong domain"
  threshold (0.05), the composite score is capped at `domain_cap` (0.40 by default),
  regardless of how well other factors scored.
- **Seniority gate** — if `seniority_match` is exactly 0 (a confirmed seniority
  mismatch), the composite is multiplied by `seniority_multiplier` (0.55 by default).
- Both gates are **re-applied after personalization** (`cap_personalization_at_gates`),
  so positive feedback signals cannot lift a confirmed off-domain or wrong-seniority job
  back above its gated ceiling.

### The requirements gate (separate from the weighted score)

`src/requirements/gate.py`'s `HardConstraintGate` runs *before* scoring and can reject a
job outright, independent of how well it would otherwise score:

- **Education**: a level above `profile.max_education` that the posting *requires*
  ("Bac+5 requis", "Bac+4 ou équivalent") is a hard rejection; a level it only wishes
  for ("Bac+5 souhaité", "un plus") is a soft penalty (`education_preferred_penalty`,
  default 20%); a level it calls non-eliminatory ("non éliminatoire", "not required")
  or mentions without any qualifier changes nothing.
- **Experience**: mandatory years above your profile's max is a hard rejection;
  preferred excess is a soft 20% penalty.
- **Principal skill**: a principal skill named in the job title that isn't in your
  profile is a hard rejection.
- **Mandatory skills in the description**: mandatory + principal → hard rejection;
  mandatory + secondary → soft 40% penalty.

These behaviors are individually toggleable via `profile.matching.education_hard_constraint`,
`principal_skill_gate`, and `mandatory_central_skill_gate`.

### Score tiers

`score_to_tier()` (the single canonical implementation other code imports) classifies the
final score:

| Tier | Threshold |
|---|---|
| `exceptional` | ≥ 80 |
| `excellent` | ≥ 72 |
| `strong` | ≥ 65 |
| `relevant` | ≥ 60 |
| `low_priority` | below 60 |

### Semantic contribution

Semantic similarity is blended in as `final = (1 − w) × deterministic + w × semantic`,
where `w` is `similarity.weight` in `config/semantic_config.yaml` — currently tuned down
to **0.06** in the shipped config (reduced from a 0.2 code default after a benchmark
showed weak correlation with domain relevance). Semantic scores are rescaled against a
fixed floor/ceiling (`reference_floor`/`reference_ceiling`) rather than per-run min-max,
and scores below `min_threshold` (0.3) are zeroed before blending.

## Collectors

`src/collectors/registry.py` maps a `type` string in `config/sources.yaml` to a collector
class. A shared retry/backoff/rate-limit layer lives in `src/collectors/base.py`
(`_fetch_with_retry` retries on 408/429/500/502/503/504 with exponential backoff and
`Retry-After` support).

### Configured in `config/sources.yaml`

| Source | Type | Method | Auth | Status |
|---|---|---|---|---|
| France Travail | `france_travail` | Official REST API (OAuth2) | `FRANCE_TRAVAIL_CLIENT_ID`/`SECRET` | **Active** — exhaustive pagination, bounded concurrency (`max_workers`, default 4) |
| RemoteOK | `remoteok` | REST API | None | **Active** |
| Jobicy | `jobicy` | REST API | None | **Active** |
| We Work Remotely | `weworkremotely` | RSS/Atom | None | **Active** |
| Indeed | `indeed` | Unofficial (Indeed's mobile API, read through python-jobspy) | none; opt-in: `unofficial_sources.indeed` in `data/settings.json` (an older `INDEED_API_KEY` counts as opted in) | **Active when chosen** — jobspy pages each query |
| EURES | `eures` | Public REST API | None | **Active** |
| Bundesagentur für Arbeit | `bundesagentur` | REST API | None | Disabled — Germany-only; every result fails this project's location filter |
| Arbetsförmedlingen | `arbetsformedlingen` | REST API | None | Disabled — Sweden-only, same reason |
| HelloWork | `hellowork` | HTML + JSON-LD scraping | None | Disabled in the main registry — production HelloWork collection happens through the separate scanner below |

### Registered but not configured (no `sources.yaml` entry)

`greenhouse`, `lever`, `smartrecruiters`, and `workingnomads` collector classes exist,
are tested, and work — they simply require a per-company board identifier that hasn't
been configured yet. Add a source entry with the relevant `board_identifier` to enable
one.

### Quarantined (non-functional, kept for reference only)

`src/collectors/experimental/` holds collector code that doesn't work and is explicitly
excluded from the registry — currently a LinkedIn collector class that targets a
nonexistent public endpoint. It cannot be enabled via configuration.

### Background scanners (outside the main collector registry)

Two sources run as independent, decoupled subsystems rather than as part of the
`config/sources.yaml`-driven collection stage — each has its own config file, staging
database, and checkpoint/resume logic:

| Scanner | Config | Notes |
|---|---|---|
| **LinkedIn** (`src/linkedin/scanner.py`) | `config/linkedin.yaml` | Uses `python-jobspy` (the `linkedin` optional dependency); lazily imported with a clear install error if missing; enabled in the shipped `config/linkedin.yaml`; unofficial, opt-in: daily once chosen (`linkedin_daily` ships with `startup_policy: when_opted_in`); the first 429 stops the scan, which resumes from its checkpoint after a cooldown of 1 h doubling to 24 h, paced slower at first (F6.4). Wired into the API via `src/api/routes/linkedin.py` (`/api/linkedin/status`, `/scan`, `/pause`, `/resume`, `/scans`), not through `CollectorRegistry`. A scan can be paused and resumed from its last checkpoint (issue #81) |
| **HelloWork** (`src/hellowork/scanner.py`) | — | Runs on its own daily schedule once chosen (`when_opted_in`), decoupled from the main pipeline; stops on the first 429 and resumes after a cooldown, like LinkedIn. Wired into the API via `src/api/routes/hellowork.py` (`/api/hellowork/status`, `/scan`, `/pause`, `/resume`, `/scans`); pausable/resumable like LinkedIn |

Both scanners write to their own staging tables; a `StagedSourceAdapter` bridges their
output back into the main pipeline as a regular source.

### Enabling a source

Set `active: true` on its entry in `config/sources.yaml` (or add one, for the
registered-but-unconfigured collectors above).

## Caching

Repeated pipeline runs are substantially faster thanks to persistent caches, all stored
in `jobs.db`:

| Cache | What is stored | Invalidation |
|---|---|---|
| Job embedding cache | Embedding vector per job, keyed by content hash + model name | Job content or embedding model change |
| Profile embedding cache | Embedding vector for the candidate profile text | Profile or model change |
| Personalization cache | Computed personalization adjustment per job | Profile or feedback history change |
| AI analysis cache (`src/ai/storage.py`) | Phase 2 / eligibility-gate results, keyed by job content hash + profile version | Job content or profile change |

On a warm cache (unchanged jobs and profile), semantic matching and personalization
approach zero compute time.

## Provenance and migrations

Each stored offer has one `job_provenance` row per source it was seen on (issue #86): its own
source with its id and URL, and every source deduplication merged into it, with first and
latest sighting. Sources are ordered as configured before deduplication, so a cross-source
duplicate keeps the same offer from one run to the next. `GET /api/jobs/{id}` returns them as
`provenance`; the job page shows "Seen on …".

Offers expire (issue #86, `Storage.apply_expiry`, run after storage): an offer whose end
date (`valid_through`, read from schema.org `validThrough` when a source gives one) has passed
is `expired` (reason `valid_through`); an active offer no longer listed by a source that was
fully collected this run (completed, not request-capped; LinkedIn and HelloWork never count)
gets a missed run, and `expire_after_missed_runs` in a row (`config/retention.yaml`, 3)
expire it (`not_seen`). Seen again, it is active again. `POST /api/jobs/{id}/availability`
`{"available": false}` records the user's report (`user_report`), which a new sighting does
not undo. Expired offers are kept, hidden from `GET /api/jobs` unless `include_expired=true`.

Schema changes live in `src/storage/migrations.py`: additive columns (`ensure_columns`) and
named one-shot data migrations recorded in `db_migrations`. Before data migrations run on a
database of 200 MB or more, it is copied to `data/backups/` (the last 2 are kept); without
enough free space the migrations wait for a later start. Rehearsed on a copy of a 3.5 GB
database (39 035 offers): 15 s, backup included, +10 MB.

## Background Scheduler

`config/schedules.yaml` configures a generic scheduler service (started automatically
with the API) that can trigger recurring pipeline runs, independent of the web UI or
CLI. Status and manual triggers are exposed at `/api/scheduler/status`,
`/api/scheduler/executions`, and `/api/scheduler/run/{definition_id}`.

`pipeline_daily` (source `pipeline`, `src/scheduler/service.py` `PipelineHandler`, issue #76)
runs the main pipeline once per UTC day with `startup_policy: when_profile_saved`: only once
`config/profile.local.yaml` exists, so a first start on the example profile sends no query.
Days missed while the backend was stopped give one catch-up run. Its automatic run waits while
a scan runs or was dispatched in the same tick; a completed scan starts the pipeline through
this schedule (trigger `after_scan`), so that run counts for the day. A run started with
`POST /api/pipeline/run` is recorded on the schedule too (trigger `manual`,
`SchedulerService.adopt_pipeline_run`): once completed, it is the day's run. `retry_max: 1` after
30 minutes; a backend shutdown during the run records it as `interrupted`. Background runs do
not use the local LLM (`use_ai` false).

Every schedule's "day" (issue #80) is the period starting at the latest local
`background.daily_time` from `data/settings.json` (default 08:00, system clock and its DST
rules; `period_start` in `src/scheduler/service.py`), and retries count per period.
`background.enabled: false` stops every automatic run; manual ones still work.
`GET/PUT /api/settings/background` read and change both.

## Frontend

React 19 + TypeScript, built with Vite, using React Query for data fetching and
plain CSS custom properties for theming (no CSS framework).

| Route | Page | Shows |
|---|---|---|
| `/` | Dashboard | Live pipeline funnel via Server-Sent Events, run trigger, scanner status cards |
| `/pipeline` | Pipeline | Run history, per-run funnel chart, scanner status/history |
| `/jobs` | Jobs | Filterable, paginated job list with tier badges |
| `/jobs/:id` | JobDetail | Full job detail, score explanation, application-status update |
| `/stats` | Stats | Score-tier distribution, top skills, rejection reasons |
| `/review` | Review | Review queue: decisions flagged for a human (disagreement/abstention, ADR 0010), a few audit-sampled rule-backed rejections, and feedback (want / not for me / not sure) |
| `/skills` | Skills | Skill graph built from collected jobs |
| `/lab` | Lab | Read-only view of lab runs, experiments, memory and the active strategy (ADR 0011) |
| `/profile` | Profile | Profile editor with ESCO-backed skill/occupation autocomplete; saves to `config/profile.local.yaml` |

## API

FastAPI backend under `src/api/`. A representative (not exhaustive) list of endpoints,
grouped by route module in `src/api/routes/`:

- **profile** — `GET /api/profile`, `PUT /api/profile`
- **pipeline** — `POST /api/pipeline/run`, `GET /api/pipeline/runs`, `GET /api/pipeline/runs/{id}`,
  `GET /api/pipeline/events` (SSE), `GET /api/pipeline/runs/{id}/jobs`
- **jobs** — `GET /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs/{id}/explanation`,
  `PATCH /api/jobs/{id}/application-status`, `POST /api/jobs/{id}/events`, `GET /api/jobs/stats`,
  `GET /api/jobs/countries`
- **dashboard** — `GET /api/dashboard`
- **esco** — `GET /api/esco/skills`, `GET /api/esco/occupations`, `GET /api/esco/status`
- **departments** — `GET /api/departments`, `GET /api/departments/all`
- **linkedin** / **hellowork** — `GET .../status`, `POST .../scan`, `GET .../scans`
- **scheduler** — `GET /api/scheduler/status`, `GET /api/scheduler/executions`, `POST /api/scheduler/run/{id}`
- **feedback** — `GET /api/jobs/feedback/vocabulary`, `GET`/`POST /api/jobs/{id}/feedback`
- **decisions** — `GET /api/jobs/{id}/decision`, `GET /api/decisions/review-queue`, `GET /api/decisions/{id}`,
  `GET /api/decisions/{id}/snapshot`, `GET /api/pipeline/runs/{id}/decisions`
- **skills** — `GET /api/skills/graph`
- **lab** (read-only) — `GET /api/lab/runs`, `/experiments` (each with a recommended `next_action`), `/memory`,
  `/active-strategy`, `/self-check` (latest self-check report and earlier statuses)
- **quality** — `GET /api/quality/health`, `POST /api/quality/jobs` (`{kind: self_check | evaluation}`, one at a
  time, 409 while one runs), `GET /api/quality/jobs`, `GET /api/quality/jobs/{id}`, `POST /api/quality/jobs/{id}/cancel`
- **health** — `GET /api/health` (no database access; used by the launcher)

At startup, `src/api/app.py` initializes/recovers the databases, seeds the bundled ESCO
data if missing, starts the scheduler, and serves
the built frontend as a SPA from `frontend/dist/` if present. Pipeline runs execute on a
background daemon thread (`src/api/runner.py`), one at a time; progress is written to
the run's database row and mirrored over SSE for the dashboard.

## Database

A single SQLite database, **`data/jobs.db`**, in WAL mode (`synchronous=NORMAL`,
`busy_timeout=5000ms`), holds nearly all application state: jobs (with an FTS5
full-text index), pipeline run history, per-run job scores, rejection records, source
checkpoints, embedding caches, personalization data, notification history (kept, no longer written), and the
LinkedIn/HelloWork staging tables. A separate **`data/esco.db`** holds the ESCO skill
taxonomy. Both are created automatically on first run under `data/`, which is not
tracked in git.

There is no versioned migration framework — schema evolution is handled by idempotent
`ALTER TABLE ... ADD COLUMN` checks at startup plus a small table of one-time named data
migrations that have already run. `src/storage/migrate.py` additionally provides a
standalone, manually-invoked script for collapsing legacy random-UUID job rows into the
current stable-hash ID scheme.
