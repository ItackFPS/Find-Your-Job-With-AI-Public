# Architecture

This document is the detailed, code-level companion to the README's architecture
overview. The README is the place to start; this file goes deeper on specific stages.
The reasons behind the durable decisions described here are recorded as ADRs in
[`docs/adr/`](adr/README.md).

## Pipeline Flow

```text
DISCOVERY / QUERY EXPANSION
    ↓
COLLECTION
    ↓
NORMALIZATION
    ↓
VALIDATION
    ↓
DEDUPLICATION
    ↓
HARD CONSTRAINT FILTERING
    ↓
REQUIREMENTS GATE  (education / experience / skill hard constraints)
    ↓
LLM ELIGIBILITY GATE  (optional — no-op unless enabled)
    ↓
DETERMINISTIC SCORING
    ↓
STORAGE  (SQLite jobs.db — canonical job record + deterministic score)
    ↓
LOCAL AI ENRICHMENT  (optional, --ai / use_ai — Phase 2)
    ↓
SEMANTIC MATCHING  (local sentence embeddings)
    ↓
PERSONALIZED RANKING  (feedback-driven adjustment; re-applies scoring gates)
    ↓
OUTPUT  (Web UI dashboard / CLI / Discord / n8n)
```

The pipeline is implemented in `src/pipeline/orchestrator.py` and shared between the CLI
entry point (`src/main.py`) and the web API (`src/api/runner.py`). Business logic lives
in the orchestrator exactly once.

**Storage timing note**: the job's canonical record and deterministic score are
persisted to `jobs.db` immediately after scoring — *before* local AI enrichment,
semantic matching, and personalization run. Those three later stages operate on that
run's in-memory job list and produce a per-run ranking (final blended score, semantic
score, personalization adjustment) recorded in the `job_scores` table; they do not
mutate the job's stored deterministic score.

## Decision Journal

`src/decisions/` records, for every job in every run — accepted AND rejected, plus jobs
dropped by validation/deduplication — one `DecisionRecord` in `job_decisions` (same
`jobs.db`): outcome, deciding stage, `strategy_version`, deterministic score, typed
`EvidenceItem`s (three-valued: SATISFIED/VIOLATED/UNKNOWN, with source rule/version and,
when quoting the job, the text span), conflicts, per-stage trace, and the exposure the job
received (rank, tier). Rejected jobs also keep a content-addressed, compressed text
snapshot (`job_snapshots`) so false negatives can be audited; text is purged after
`text_retention_days` while the decision row and snapshot metadata stay. The
`strategy_version` is a hash of the profile, filters, scoring, semantic and personalization
config plus rule/prompt versions and the run mode (manifest in `strategy_versions`).

It is observability only: `DecisionRecorder` reads what each stage produced, never
changes a decision, score or ranking, and is fail-open (errors land in
`stats.decisions.error`). Read-only API: `GET /api/jobs/{id}/decision`, `GET /api/decisions/{id}`
(+ `/snapshot` for the kept text of a rejected job), `GET /api/pipeline/runs/{id}/decisions`; the job
detail page shows the record (supporting / against / unknown / conflicts). Configuration:
`config/decisions.yaml`. See
[ADR 0008](adr/0008-decision-journal.md).

## Disagreement and abstention

`src/decisions/disagreement.py` compares the deterministic fit, semantic similarity, Phase 2
profile match, the LLM eligibility verdict, UNKNOWN density, evidence completeness and
contradictions and answers `accept | review | reject` with an uncalibrated, agreement-based
`decision_confidence` (never the LLM's own stated confidence). It starts in **shadow** mode
(`config/disagreement.yaml`): recorded in the journal, nothing changes; `review` never removes a
job from ranking or notifications. See [ADR 0010](adr/0010-disagreement-and-abstention.md).

## Feedback Journal

`src/feedback/` stores user signals append-only in `feedback_events` (`jobs.db`): explicit
feedback with structured reasons (`POST /api/jobs/{id}/feedback`, UI on the job detail page),
behaviour (viewed, apply link opened) and outcomes (application-status transitions, with the
legacy value preserved and a canonical value derived by an explicit mapping). Every event
carries its provenance, timestamp, signal class and the `decision_id` / `strategy_version` it
reacted to; no weight is computed at insertion. See [ADR 0009](adr/0009-feedback-journal.md)
and `config/feedback.yaml`.

## Career Skill Graph

`src/skillgraph/graph.py` (`GET /api/skills/graph`, **Skills** page) is read-only analytics over the
stored jobs (opened with a `mode=ro` connection) and the profile's `target_skills`: technologies tagged
on jobs in a time window (restricted to the `KNOWN_TECHNOLOGIES` vocabulary the requirements gate and
Stats page use), their demand among *relevant* jobs (latest final score ≥ 0.65, or marked interested /
acted on), co-occurrence edges, and gaps — unlisted skills ranked by relevant demand, each with its
evidence and the owned skills it co-occurs with. It never influences filtering, scoring or ranking,
and returns its limits with every response (tag coverage, target ≠ proficiency, rejected postings
not counted).

## Component Overview

| Stage | Module | Purpose |
|---|---|---|
| Discovery | `src/discovery/registry.py` | Loads and validates source configurations from `config/sources.yaml` |
| Query expansion | `src/discovery/query_expansion.py` | Generates multilingual search terms per source: a hardcoded baseline plus optional profile-/LLM-derived additions, capped at `max_queries` (default 12) |
| Collection | `src/collectors/*.py` | Fetches raw jobs from each active source; never raises — returns a `CollectorResult` with `.errors` populated on failure |
| Normalization | `src/pipeline/normalize.py` | Maps source-specific data to the unified `Job` Pydantic model |
| Validation | `src/pipeline/validate.py` | Checks required fields and data integrity |
| Deduplication | `src/pipeline/deduplicate.py` | Intra- and cross-source deduplication via a deterministic identity hierarchy |
| Filtering | `src/pipeline/filter.py` | Ordered hard-constraint checks: location, remote, work_mode, seniority, published_age, salary, title_keywords, contract_type — first failure rejects the job |
| Requirements gate | `src/requirements/gate.py` (`HardConstraintGate`) | Hard/soft rejection on stated education, years of experience, and principal/mandatory skills vs. the profile. Failures are caught and treated as pass-through (recorded in `stats.requirements_gate_error`), never fatal |
| LLM eligibility gate | `src/ai/eligibility.py` (`EligibilityGate`) | Optional local-LLM check for implicit requirement mismatches the deterministic gate can't see. `run_stage()` is called unconditionally by the orchestrator but returns immediately (`ran=False`) unless both `use_ai` and `profile.matching.llm_eligibility_enabled` are true. Fail-open on LLM error |
| Scoring | `src/pipeline/scoring.py` | 5-factor weighted composite (location/domain/skills/seniority/experience) with domain and seniority hard gates, plus soft penalties carried over from the requirements gate and a language-mismatch penalty |
| Storage | `src/storage/sqlite.py` | Batch upsert into SQLite with an FTS5 full-text index; runs immediately after scoring |
| Local AI enrichment (Phase 2) | `src/ai/pipeline.py` (`Phase2Pipeline`) | Analyzes only the top `PHASE2_MAX_CANDIDATES` (default 100) jobs by deterministic score via a local LLM; builds enriched text that feeds semantic matching. Only runs if `use_ai` is true; any failure is caught and logged, pipeline continues |
| Semantic matching | `src/pipeline/semantic_matching.py` | Cosine similarity between job and profile embeddings (`paraphrase-multilingual-MiniLM-L12-v2`), with a persistent SQLite-backed cache |
| Personalization | `src/personalization/engine.py` | Feedback-driven score nudges with a persistent bulk cache; `cap_personalization_at_gates()` re-applies the scoring stage's domain/seniority gates afterward so feedback cannot undo them |
| CLI output | `src/notifications/cli.py` | Terminal summary of pipeline results and top-ranked jobs |
| Web API | `src/api/app.py` | FastAPI application serving REST endpoints, background workers, and the built React frontend |
| Discord notifications | `src/notifications/` | Optional routing of new/top jobs and pipeline events to Discord webhooks |
| n8n integration | `config/n8n_weekly_digest.json`, `src/api/routes/jobs.py`, `src/api/auth.py` | Optional weekly email digest, triggered externally by n8n against dedicated, optionally-authenticated API endpoints |

## Collectors

`src/collectors/registry.py` maps source type names to collector implementations. The
orchestrator loads collectors dynamically based on the `type` field in
`config/sources.yaml`. A shared retry/backoff/rate-limit layer lives in
`src/collectors/base.py` (`BaseCollector._fetch_with_retry`).

| Type | File | Status |
|---|---|---|
| `france_travail` | `france_travail.py` | Active — OAuth2 REST API, bounded concurrency, exhaustive pagination |
| `remoteok` | `remoteok.py` | Active — unauthenticated REST API |
| `jobicy` | `jobicy.py` | Active — unauthenticated REST API |
| `weworkremotely` | `weworkremotely.py` | Active — RSS/Atom feeds |
| `indeed` | `indeed.py` | Active — unofficial mobile GraphQL API, cursor-based pagination |
| `eures` | `eures.py` | Active — public REST API |
| `apec` | `apec.py` | Active — public REST search endpoint |
| `bundesagentur` | `bundesagentur.py` | Disabled — Germany-only, fails this project's location filter |
| `arbetsformedlingen` | `arbetsformedlingen.py` | Disabled — Sweden-only, same reason |
| `wttj` | `wttj.py` | Disabled — Crawl4AI/Playwright-based; blocked by AWS WAF bot protection as of the last live check (2026-08-27) |
| `hellowork` | `hellowork.py` | Disabled in the registry — production HelloWork collection uses the separate scanner below |
| `greenhouse` | `greenhouse.py` | Registered, working, not configured — needs a `board_identifier` |
| `lever` | `lever.py` | Registered, working, not configured — needs a company slug |
| `smartrecruiters` | `smartrecruiters.py` | Registered, working, not configured |
| `workingnomads` | `workingnomads.py` | Registered, working, not configured |
| `remote` / `rss` | `remote.py` | Generic RSS/REST collector, registered under both type names, not currently configured |
| — | `experimental/linkedin.py` | Quarantined — not in the registry, targets a nonexistent endpoint, cannot be enabled via config |

Two sources are collected by fully independent scanner subsystems instead of through
this registry:

| Scanner | Module | Config | Notes |
|---|---|---|---|
| LinkedIn | `src/linkedin/scanner.py` | `config/linkedin.yaml` | Uses `python-jobspy` (lazy import, `linkedin` extra); own staging DB + checkpoint/resume |
| HelloWork | `src/hellowork/scanner.py` | — | Own daily schedule, own staging DB |

Both bridge back into the main pipeline's job set via a `StagedSourceAdapter`.

## Deduplication Identity Hierarchy

**Intra-source** (same `source` value):

1. `(source, source_job_id)` — definite duplicate; the source guarantees uniqueness.
2. `(source, canonical_url)` — definite duplicate when `source_job_id` is absent.
3. Neither available — keep unconditionally; do not merge on title or company alone.

**Cross-source** (different `source` values):

1. Normalized `canonical_url` — globally unique identifier.
2. `(company_norm, title_norm, location_norm, employment_type)` — content identity,
   **only when company is non-empty**. Jobs from anonymous/unknown employers are never
   merged on content alone.

This prevents false deduplication of legitimate postings that share a title but differ
in location, city, or contract type.

## Requirements Gate

`src/requirements/gate.py`'s `HardConstraintGate` runs after hard filtering and before
scoring, and can reject a job regardless of how well it would otherwise score:

- **Education**: a level above `profile.max_education` that the posting *requires*
  (mandatory wording, or stated as the bar: "Bac+4 ou équivalent") → hard reject; a
  level only wished for ("souhaité", "apprécié", "un plus") or an LLM-only ("core")
  reading → soft penalty (`education_preferred_penalty`, default 20%); explicitly
  non-eliminatory ("non éliminatoire", "not required") or unqualified mentions →
  nothing. See ADR 0004.
- **Experience**: mandatory years above the profile's max → hard reject; preferred
  excess → soft 20% penalty.
- **Principal skill in the title** not present in the profile → hard reject.
- **Mandatory skill in the description**: mandatory + principal → hard reject;
  mandatory + secondary → soft 40% penalty.

Individually toggleable via `profile.matching.education_hard_constraint`,
`principal_skill_gate`, and `mandatory_central_skill_gate`.

## Scoring Architecture (Phase 1/5)

`src/pipeline/scoring.py` computes a weighted composite from five factors, then applies
hard gates:

| Factor | Default weight |
|---|---|
| `location_fit` | 0.30 |
| `domain_relevance` | 0.25 |
| `skills_match` | 0.20 |
| `seniority_match` | 0.15 |
| `experience_fit` | 0.10 |

- **Domain gate**: `domain_relevance ≤ 0.05` caps the composite at `domain_cap` (0.40).
- **Seniority gate**: `seniority_match == 0.0` multiplies the composite by
  `seniority_multiplier` (0.55).
- Both gates are re-applied after personalization (`cap_personalization_at_gates`).
- `score_to_tier()` classifies the final score into `exceptional` (≥80), `excellent`
  (≥72), `strong` (≥65), `relevant` (≥60), or `low_priority`. This is the single
  canonical implementation; other consumers import it, except
  `frontend/src/utils/scoreToTier.ts`, a documented manual mirror kept in sync by hand.

## Embedding Layer (Phase 3)

`src/embeddings/embedding.py` wraps `sentence-transformers` (in-memory LRU-ish cache,
SHA-256 keyed). The default model is `paraphrase-multilingual-MiniLM-L12-v2`
(384-dimensional, multilingual, CPU-ready, ~470 MB on first download). The *durable*
cache lives in `Storage` (SQLite `job_embeddings` table, keyed by job id + model name +
content hash), managed by `src/pipeline/semantic_matching.py`, not by `EmbeddingService`
itself.

The semantic matching stage:
1. Builds/reuses a cached profile embedding (`build_profile_embedding`), keyed by
   profile content hash + model name.
2. Computes cosine similarity between each job and the profile embedding.
3. Rescales the raw similarity against a fixed floor/ceiling
   (`similarity.reference_floor`/`reference_ceiling` in `config/semantic_config.yaml`),
   zeroing scores below `similarity.min_threshold` (0.3).
4. Blends: `final = (1 − w) × deterministic + w × semantic_normalized`, where `w` is
   `similarity.weight` — shipped at **0.06** (tuned down from a 0.2 code default after a
   benchmark found weak correlation with domain relevance).

## Personalization Layer (Phase 4)

`src/personalization/` stores feedback events (interested, applied, not_interested,
ignored, rejected) in SQLite and computes bounded score adjustments from matching
signals (company, skills, title keywords, remote status). A persistent bulk cache
avoids recomputing adjustments for unchanged jobs across runs.
`cap_personalization_at_gates()` re-applies scoring's domain/seniority gates
post-adjustment so positive feedback cannot lift a confirmed off-domain or
wrong-seniority job back above its gated ceiling.

## Caching Strategy

| Cache | Key | Invalidation |
|---|---|---|
| Job embedding | job content hash + model name | Content or model change |
| Profile embedding | profile YAML hash + model name | Profile or model change |
| Personalization | job id + profile hash | Profile or feedback history change |
| AI analysis (Phase 2 / eligibility) | job content hash + profile version | Job content or profile change |

## AI / LLM Layers (optional)

Two independent, opt-in stages, both targeting an OpenAI-compatible local chat
completions API (LM Studio or Ollama) via `LocalLLMProvider`
(`src/ai/interfaces.py`):

- **LLM eligibility gate** (`src/ai/eligibility.py`) — runs after the deterministic
  requirements gate, before scoring. No-op unless `use_ai` **and**
  `profile.matching.llm_eligibility_enabled` are both true. Fail-open.
- **Local AI enrichment / Phase 2** (`src/ai/`) — runs after storage, before semantic
  matching. Only the top `PHASE2_MAX_CANDIDATES` jobs by deterministic score are
  analyzed. Only runs if `use_ai` is true (CLI `--ai`, or the API's `use_ai` request
  field — **not** an environment variable). Configuration is entirely
  environment-variable driven via `PHASE2_*` vars (`src/ai/config.py`); there is no
  YAML config file for this. Non-blocking: any failure is logged and the run continues
  without AI enrichment.

Both stages cache results keyed by job content hash + profile version
(`src/ai/storage.py`), so repeated runs only pay the LLM cost for changed jobs.

## Discord Notifications

`config/notifications.yaml` defines four static channels (`pipeline`, `errors`,
`statistics`, `system-status`), per-location channels, and per-location "Top 10"
leaderboard channels. `src/notifications/discord.py`'s `DiscordProvider` resolves each
channel's webhook URL with this precedence: channel-specific env var → constructor
value → legacy `DISCORD_WEBHOOK_URL` fallback (static channels only — location channels
never fall back, to avoid misrouting).

`src/pipeline/location_match.py`'s `resolve_job_location()` maps a job to a location
slug (e.g. `dept-83`) by matching its department/remote/country data against the
profile's ordered `geography.location_preferences`. `src/notifications/top10_query.py`
computes each location's top-10 leaderboard directly from the `jobs` table.

Two background daemon threads, both started automatically by `src/api/app.py`:
- `DiscordReconcilerWorker` — ticks every `DISCORD_RECONCILER_INTERVAL_SECONDS`
  (default 300s), calling `src.discord_admin.service.run_reconciliation_pass()` to
  create/adopt Discord channels and webhooks for the profile's current locations. Only
  functional if `DISCORD_BOT_TOKEN`/`DISCORD_GUILD_ID` are set; otherwise no-ops safely.
- `SystemStatusWorker` — ticks every `SYSTEM_STATUS_INTERVAL_SECONDS` (default 300s),
  editing one standing message in `#system-status`.

Duplicate sends are prevented by a partial unique index on
`notification_history(provider, event_type, entity_id) WHERE status='sent'`.

`src/discord_admin/` is a separate MCP server (the `mcp_admin` extra; never imported by
the main backend) exposing the same reconciliation logic as read/administer tools for
an agent session — `list_channels`, `list_webhooks`, `get_reconciliation_plan`,
`reconcile_now`.

## n8n Integration

`config/n8n_weekly_digest.json` is an importable n8n workflow (cron: Monday 10:00
Paris) implementing a weekly email digest, with its own idempotency guard via n8n
workflow static data. It calls three API endpoints — `GET /api/jobs/digest/weekly`,
`GET /api/jobs/digest/weekly-combined`, `GET /api/pipeline/runs/{run_id}/jobs/n8n` —
each gated by `src/api/auth.py`'s `require_n8n_api_key`, a no-op unless
`N8N_CALLER_API_KEY` is set (in which case it requires a matching `X-N8N-API-Key`
header, compared in constant time). n8n itself runs externally; nothing in this repo's
Docker Compose setup starts or manages an n8n instance.

## Web API and Frontend

`src/api/app.py` is a FastAPI application that, at startup:

- Initializes/recovers the job and run databases (including stale "running" row
  recovery via PID + heartbeat).
- Seeds the bundled ESCO dataset if the ESCO database is empty.
- Starts the generic background scheduler, the Discord reconciler worker, and the
  system-status worker.
- Exposes REST endpoints under `/api/` for jobs, pipeline control, profile, dashboard,
  ESCO, departments, the LinkedIn/HelloWork scanners, and the scheduler.
- Serves the built React frontend as a SPA from `frontend/dist/` in production, if
  present.
- `GET /api/lab/*` (`src/api/routes/lab.py`, **Lab** page) shows evaluation run results
  (`data/benchmarks/runs/`), lab experiments with verdicts, guardrails and metric deltas, the lab
  agent's memory and the `active_strategy` pointer history. It reads those files with read-only
  SQLite URIs and plain JSON and imports neither `src.evaluation` nor `src.lab`.

`src/api/runner.py` runs the pipeline on a background daemon thread (one run at a time),
writing progress to the run's database row and mirroring it over Server-Sent Events for
the dashboard — there is no websocket and no separate polling loop in the runner itself.

The React frontend (`frontend/`) is built with Vite, TypeScript, and React Query;
styling uses plain CSS custom properties, not a CSS framework.

## Database

A single SQLite database, `data/jobs.db` (WAL mode, `synchronous=NORMAL`,
`busy_timeout=5000ms`), holds jobs (+ FTS5 index), pipeline run history, per-run job
scores, rejection records, source checkpoints, embedding caches, personalization data,
notification history, and the LinkedIn/HelloWork staging tables. `data/esco.db` holds
the ESCO taxonomy separately. There is no versioned migration framework: schema
evolution uses idempotent `ALTER TABLE ADD COLUMN` checks plus a small ledger of
one-time named data migrations already applied. `src/storage/migrate.py` additionally
provides a standalone, manually-run tool for collapsing legacy random-UUID job rows
into the current stable-hash ID scheme.

## Configuration

All user-tunable parameters live in `config/`. For `profile.yaml` and
`notifications.yaml`, a gitignored `<name>.local.yaml` sibling replaces the tracked
default when present; readers resolve it at call time and the application writes only
the local file (`src/config_files.py`).

| File | Purpose |
|---|---|
| `config/profile.yaml` | Candidate profile: target domains, skills, geography, contract types, seniority/experience bounds, languages, requirements-gate toggles |
| `config/sources.yaml` | Job sources: name, type, URL, active flag, per-source settings |
| `config/filters.yaml` | Hard constraints: domain/contract keywords, seniority exclusions, title exclusions, remote scope |
| `config/scoring.yaml` | Score weights and gate thresholds |
| `config/semantic_config.yaml` | Embedding model, similarity weight/threshold, normalization anchors |
| `config/personalization.yaml` | Feedback weights, feature weights, adjustment bounds |
| `config/notifications.yaml` | Discord channels, n8n toggle, routing rules |
| `config/linkedin.yaml` | LinkedIn background scanner settings |
| `config/schedules.yaml` | Background scheduler definitions |
| `config/retention.yaml` | Pipeline-run history retention policy |
| `config/n8n_weekly_digest.json` | Importable n8n workflow for the weekly digest |
