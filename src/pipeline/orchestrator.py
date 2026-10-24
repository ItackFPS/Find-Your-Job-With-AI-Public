"""Shared pipeline orchestration service.

Both the CLI (src/main.py) and the web API (src/api/runner.py) call this
module.  Business logic lives here exactly once; neither entry point
duplicates the pipeline stages.

Pipeline stages (in order):
  collect → normalize → validate → deduplicate → filter →
  score → semantic → personalization → store
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    from src.collectors.base import BaseCollector
    from src.models.job import Job
    from src.storage.sqlite import Storage

from src.config_files import resolve_config_path

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class ScoredJobRecord:
    """All score data for a single job that passed the pipeline."""

    job_id: str
    job_title: str | None
    job_company: str | None
    job_source: str
    final_score: float
    deterministic_score: float
    semantic_score: float
    factors: dict[str, float]
    personalization_adjustment: float


@dataclass
class RejectionRecord:
    """A job that was rejected, with the reason."""

    job_id: str | None
    title: str | None
    company: str | None
    location: str | None
    source: str | None
    rejection_stage: str
    rejection_detail: str


@dataclass
class PipelineStats:
    collected: int = 0
    validated: int = 0
    deduplicated: int = 0
    filtered: int = 0
    # Fine-grained breakdown of the two independent stages that both feed
    # `filtered`: the domain/keyword hard filter (src/pipeline/filter.py)
    # and the requirements gate (src/requirements/gate.py). Neither stage
    # duplicates the other's work; these fields exist purely to make the
    # two gates individually visible instead of collapsing into one number.
    hard_filter_input: int = 0
    hard_filter_passed: int = 0
    requirements_gate_input: int = 0
    requirements_gate_passed: int = 0
    # Set when the gate itself raised and was bypassed (all input jobs pass
    # through untouched) — distinguishes "gate ran, nothing violated" from
    # "gate crashed and was skipped" for consumers of requirements_gate_passed.
    requirements_gate_error: str | None = None
    # Lightweight LLM semantic eligibility gate (src/ai/eligibility.py) —
    # optional, runs on requirements-gate survivors, before scoring. Follows
    # the same input/passed shape as the two gates above so the funnel stays
    # uniform; llm_eligibility carries the richer breakdown (successful/
    # failed/uncertain/cached/time_seconds/candidates_before_after_eligibility).
    llm_eligibility_input: int = 0
    llm_eligibility_passed: int = 0
    llm_eligibility_rejected: int = 0
    llm_eligibility: dict[str, Any] = field(default_factory=dict)
    scored: int = 0
    semantically_matched: int = 0
    personalized: int = 0
    duration_seconds: float = 0.0
    preflight: dict[str, Any] = field(default_factory=dict)
    by_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_country: dict[str, int] = field(default_factory=dict)
    by_work_mode: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    profile_hash: str = ""
    model_name: str = ""
    scoring_config_hash: str = ""
    phase2: dict[str, Any] = field(default_factory=dict)
    # Whether this run was invoked with Phase 2 (local AI) enabled — set at
    # the top of run_pipeline() from the use_ai argument, regardless of
    # entry point (CLI --ai, or the API's use_ai request flag), so it is
    # always accurate for this run rather than inferred from whether
    # stats.phase2 happens to be non-empty.
    use_ai: bool = False
    stage_times: dict[str, float] = field(default_factory=dict)
    embedding_cache_hits: int = 0
    embedding_cache_misses: int = 0
    personalization_cache_hits: int = 0
    personalization_cache_misses: int = 0
    # Live item-level progress for whichever stage is currently running —
    # {"stage": str, "processed": int, "total": int}. Purely observational:
    # populated by _stage_reporter() from the same in-flight counts each
    # stage already computes, never read by any filtering/scoring/ranking
    # decision. Empty between stages' live updates (the per-stage fields
    # above remain the authoritative post-stage counts regardless).
    stage_progress: dict[str, Any] = field(default_factory=dict)
    # Decision-journal summary (observability only): {enabled, recorded, snapshots, error?}.
    decisions: dict[str, Any] = field(default_factory=dict)
    # Which filters.yaml / scoring.yaml the run used: the human-approved active strategy or config/
    # (src/decisions/active_strategy.py): {status, reason, pointer_id, bundle_hash, ...}.
    active_strategy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Derived lifecycle counters for clearer funnel accounting.
        # These supplement (never replace) the existing fields so consumers
        # that already use collected / validated / deduplicated / filtered / scored
        # continue to work without change.
        validation_rejected = self.collected - self.validated
        deduplication_removed = self.validated - self.deduplicated
        filter_rejected = self.deduplicated - self.filtered
        return {
            # --- Primary pipeline counts (stable names, consumed by frontend) ---
            "collected": self.collected,
            "validated": self.validated,
            "deduplicated": self.deduplicated,
            "filtered": self.filtered,
            "scored": self.scored,
            "semantically_matched": self.semantically_matched,
            "personalized": self.personalized,
            # --- Derived rejection counts (supplementary, unambiguous names) ---
            "validation_rejected": max(validation_rejected, 0),
            "deduplication_removed": max(deduplication_removed, 0),
            # CUMULATIVE across every filtering stage that ran (hard filter +
            # requirements gate + LLM eligibility gate), by design — it is
            # `deduplicated - filtered`, and `filtered` is the survivor count
            # after whichever filter stage ran last (see the note below).
            # NOT meant to be summed with hard_filter_rejected /
            # requirements_gate_rejected / llm_eligibility_rejected — it
            # already includes them. See test_pipeline_stage_stats.py for the
            # backward-compatibility contract this preserves.
            "filter_rejected": max(filter_rejected, 0),
            # --- Three-stage filter breakdown (supplementary; `filtered` above
            # is overwritten after each filter stage in sequence — hard filter,
            # then requirements gate, then the LLM eligibility gate below — so
            # by the time this dict is built it equals `llm_eligibility_passed`
            # (the final pre-score count), not `requirements_gate_passed` —
            # `filtered` remains the authoritative "final filtered count" for
            # existing consumers, it just reflects the *last* filter stage run ---
            "hard_filter_input": self.hard_filter_input,
            "hard_filter_passed": self.hard_filter_passed,
            "hard_filter_rejected": max(self.hard_filter_input - self.hard_filter_passed, 0),
            "requirements_gate_input": self.requirements_gate_input,
            "requirements_gate_passed": self.requirements_gate_passed,
            "requirements_gate_rejected": max(self.requirements_gate_input - self.requirements_gate_passed, 0),
            "requirements_gate_error": self.requirements_gate_error,
            "llm_eligibility_input": self.llm_eligibility_input,
            "llm_eligibility_passed": self.llm_eligibility_passed,
            "llm_eligibility_rejected": self.llm_eligibility_rejected,
            "llm_eligibility": self.llm_eligibility,
            # --- Remaining fields ---
            "duration_seconds": self.duration_seconds,
            "preflight": self.preflight,
            "by_source": self.by_source,
            "by_country": self.by_country,
            "by_work_mode": self.by_work_mode,
            "errors": self.errors,
            "profile_hash": self.profile_hash,
            "model_name": self.model_name,
            "scoring_config_hash": self.scoring_config_hash,
            "phase2": self.phase2,
            "use_ai": self.use_ai,
            "stage_times": self.stage_times,
            "embedding_cache_hits": self.embedding_cache_hits,
            "embedding_cache_misses": self.embedding_cache_misses,
            "personalization_cache_hits": self.personalization_cache_hits,
            "personalization_cache_misses": self.personalization_cache_misses,
            "stage_progress": self.stage_progress,
            "decisions": self.decisions,
            # Which filters.yaml / scoring.yaml this run used (persisted so the run history can say so).
            "active_strategy": self.active_strategy,
        }


@dataclass
class PipelineResult:
    run_id: str
    started_at: datetime
    completed_at: datetime
    status: str                         # 'completed' | 'failed'
    stats: PipelineStats
    scored_jobs: list[ScoredJobRecord]
    rejection_records: list[RejectionRecord]
    error: str | None = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    db_path: str = "data/jobs.db",
    config_dir: str = "config",
    progress_callback: ProgressCallback | None = None,
    use_ai: bool = False,
    prefetched_linkedin_jobs: "list[Any] | None" = None,
    prefetched_hellowork_jobs: "list[Any] | None" = None,
    run_id: str | None = None,
) -> PipelineResult:
    """Run the full job discovery pipeline and return a structured result.

    Args:
        db_path:           Path to the SQLite database file.
        config_dir:        Directory containing profile.yaml, filters.yaml, etc.
        progress_callback: Optional callable invoked after each stage with the
                           current stage name and a serialisable stats dict.
                           Used by the API runner to persist live progress.
        use_ai:            When True, runs Phase 2 (local LLM job understanding)
                           between scoring and semantic matching.  Requires a
                           running LM Studio or Ollama server; see
                           src/ai/config.py for PHASE2_* environment variables.
                           Phase 2 is non-blocking — if the LLM is unavailable
                           the pipeline continues without AI enrichment.
        run_id:            Identifier of this run. The API runner passes the id of the run row it
                           created, so the decision journal and the run share one id; generated when
                           omitted (CLI, tests).

    Returns:
        A :class:`PipelineResult` with status, statistics, scored job records,
        and rejection records.  Never raises — failures are captured in the
        result's ``error`` field and ``status = 'failed'``.
    """
    run_id = run_id or str(uuid.uuid4())
    started_at = datetime.now(UTC)
    stats = PipelineStats()
    stats.use_ai = use_ai

    # Lazy imports keep startup fast and avoid circular dependency issues.
    from src.collectors.registry import REMOVED_SOURCE_TYPES
    from src.collectors.registry import registry as collector_registry
    from src.decisions.recorder import DecisionRecorder
    from src.discovery.registry import SourceRegistry
    from src.embeddings.embedding import EmbeddingService
    from src.personalization.config import load_personalization_config
    from src.personalization.engine import apply_personalization
    from src.personalization.storage import PersonalizationStorage
    from src.pipeline.deduplicate import deduplicate_jobs
    from src.pipeline.filter import (
        build_filter_config,
        filter_jobs,
        load_filters,
        load_profile,
    )
    from src.pipeline.normalize import normalize_jobs
    from src.pipeline.preflight import run_preflight
    from src.pipeline.scoring import (
        build_scoring_config,
        cap_personalization_at_gates,
        load_scoring_config,
        score_jobs_with_breakdown,
    )
    from src.pipeline.semantic_matching import (
        apply_semantic_matching,
        build_profile_embedding,
        integrate_semantic_into_ranking,
        load_semantic_config,
    )
    from src.pipeline.validate import validate_jobs
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    storage = Storage(db_path=db_path)
    storage.init_db()

    try:
        # =================================================================
        # PREFLIGHT (mandatory, runs once before any collection)
        # France Travail is a collection source: an active source that
        # cannot authenticate blocks the run (status FAILED). The check is
        # skipped entirely when the source is inactive in sources.yaml.
        # =================================================================
        _t_preflight = time.perf_counter()
        preflight_result = run_preflight(config_dir)
        for line in preflight_result.report_lines:
            logger.info(line)
        stats.preflight = preflight_result.to_dict()
        stats.stage_times["preflight"] = time.perf_counter() - _t_preflight
        _notify(progress_callback, "preflight", stats)

        if not preflight_result.ready:
            stats.errors.extend(preflight_result.errors)
            stats.duration_seconds = (datetime.now(UTC) - started_at).total_seconds()
            storage.close()
            return PipelineResult(
                run_id=run_id,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                status="failed",
                stats=stats,
                scored_jobs=[],
                rejection_records=[],
                error="Preflight failed: " + "; ".join(preflight_result.errors),
            )

        if preflight_result.status == "DEGRADED":
            # Non-blocking: recorded so the final result clearly shows
            # notifications could not be delivered, or that an unconfigured
            # source was skipped, without stopping the scan.
            stats.errors.extend(preflight_result.degraded_errors)
        skipped_source_types = set(preflight_result.skipped_source_types)
        skip_reasons = preflight_result.skip_reasons

        # =================================================================
        # EARLY PROFILE LOAD (for discovery query expansion)
        # The profile is also used later in the FILTER stage.
        # Loading it here is cheap (YAML read) and keeps a single instance.
        # =================================================================
        profile_path = resolve_config_path(config_dir, "profile.yaml")
        early_profile = None
        if profile_path.exists():
            # A v1 personal profile (département priorities) is converted to the distance
            # model once, with a backup (src/geo/profile_migration.py). Fail-open.
            from src.geo.profile_migration import migrate_personal_profile

            migrate_personal_profile(profile_path)
            try:
                from src.pipeline.filter import load_profile as _load_profile_fn
                early_profile = _load_profile_fn(str(profile_path))
            except Exception as exc:
                logger.warning("Could not load profile for discovery expansion: %s", exc)

        # Active strategy (ADR 0011): an approved bundle's filters.yaml / scoring.yaml replace config/'s;
        # anything doubtful (no pointer, inert, invalid or stale bundle) keeps config/. Never raises.
        from src.decisions.active_strategy import resolve_active_strategy

        active_strategy = resolve_active_strategy(db_path, config_dir)
        stats.active_strategy = active_strategy.to_dict()

        # Decision journal (observability only; fail-open — see src/decisions/recorder.py).
        recorder = DecisionRecorder.create(
            config_dir=config_dir, profile_path=profile_path, db_path=db_path, run_id=run_id, use_ai=use_ai,
            active=active_strategy,
        )

        # =================================================================
        # DISCOVERY QUERY EXPANSION (once per run, before collection)
        # Generates broad multilingual search terms from the user profile.
        # Falls back to static terms on any failure — never blocks collection.
        # =================================================================
        all_discovery_queries: dict[str, list[str]] = {}
        all_partition_vocabularies: dict[str, list[str]] = {}
        if early_profile is not None:
            try:
                from src.discovery.adaptive import compute_query_yield_history
                from src.discovery.query_expansion import DiscoveryQueryExpander
                from src.storage.runs import RunStorage

                # Phase LATER / I15: bounded, deterministic adaptive discovery —
                # reorder (never add/remove) queries by their OWN measured
                # historical yield. Best-effort: any failure here just means
                # no history is available, falling back to the exact pre-I15
                # static+profile order (never blocks/changes collection itself).
                yield_history_by_source: dict[str, dict[str, Any]] = {}
                try:
                    _hist_rs = RunStorage(db_path=db_path)
                    _hist_rs.init_db()
                    try:
                        recent_runs = _hist_rs.list_runs(limit=10)
                    finally:
                        _hist_rs.close()
                    for src_type in ("france_travail", "jobicy", "remoteok"):
                        history = compute_query_yield_history(recent_runs, src_type)
                        if history:
                            yield_history_by_source[src_type] = history
                except Exception as exc:
                    logger.warning("Adaptive discovery history lookup failed (non-fatal): %s", exc)

                expander = DiscoveryQueryExpander(
                    llm_endpoint=None,  # static + profile queries only; no LLM call
                    llm_timeout=10,
                )
                all_discovery_queries = expander.expand_all(
                    early_profile, yield_history_by_source=yield_history_by_source
                )
                logger.info("Discovery query expansion complete: %s", all_discovery_queries)
                # Build partition vocabulary for saturated-query recovery.
                for src_type in all_discovery_queries:
                    vocab = expander.generate_partition_vocabulary(early_profile, src_type)
                    if vocab:
                        all_partition_vocabularies[src_type] = vocab
            except Exception as exc:
                logger.warning(
                    "Discovery query expansion failed — collectors will use their defaults: %s",
                    exc,
                )

        # =================================================================
        # COLLECT
        # =================================================================
        _t_collect = time.perf_counter()
        sources_path = Path(config_dir) / "sources.yaml"
        source_registry = SourceRegistry(config_path=str(sources_path))
        active_sources = [
            s for s in source_registry.all_sources
            if s.get("active", True) and s.get("type") not in skipped_source_types
            and s.get("type") not in REMOVED_SOURCE_TYPES
        ]
        # A 1.x config may still list a source type removed in 2.0: skip it with
        # one line per run saying so, rather than a failure on every run.
        for s in source_registry.all_sources:
            removed_reason = REMOVED_SOURCE_TYPES.get(s.get("type", ""))
            if s.get("active", True) and removed_reason:
                logger.warning("Skipping source %s: %s", s["name"], removed_reason)
                stats.by_source[s["name"]] = {
                    "collected": 0, "accepted": 0, "errors": 0,
                    "status": "skipped", "reason": removed_reason,
                }
        # An active source left out by the preflight is listed as skipped, with what
        # to set up, so a missing credential never looks like a source that ran.
        for s in source_registry.all_sources:
            if s.get("active", True) and s.get("type") in skipped_source_types:
                stats.by_source[s["name"]] = {
                    "collected": 0, "accepted": 0, "errors": 0,
                    "status": "skipped", "reason": skip_reasons.get(s.get("type", ""), ""),
                }

        all_jobs: list[Job] = []

        def _run_source(source_cfg: dict[str, Any]) -> tuple[str, list[Job], dict[str, Any], list[str]]:
            """Collect and normalize jobs from one source; safe to run in a thread."""
            name = source_cfg["name"]
            src_type = source_cfg.get("type", "")
            try:
                collector_class = collector_registry.get_collector_class(src_type)
            except KeyError:
                return name, [], {"collected": 0, "accepted": 0, "errors": 1}, [
                    f"Unknown source type '{src_type}' for {name}, skipping"
                ]
            merged_cfg = _build_collector_config(source_cfg)
            if src_type in all_discovery_queries:
                merged_cfg["discovery_queries"] = all_discovery_queries[src_type]
            if src_type in all_partition_vocabularies:
                merged_cfg["partition_vocabulary"] = all_partition_vocabularies[src_type]
            if src_type == "france_travail_web" and early_profile is not None:
                # What to read: the profile's domains and its départements — with a home,
                # those within the maximum distance (plus home's); else the v1 list.
                from src.pipeline.location_distance import distance_config, ft_departments

                merged_cfg.setdefault("target_domains", list(early_profile.target_domains))
                distance_cfg = distance_config(early_profile)
                merged_cfg.setdefault("departments", ft_departments(distance_cfg) if distance_cfg else [
                    p.value for p in early_profile.geography.location_preferences
                    if p.type == "department" and p.status != "excluded"])
            # Registered collectors all take ``config=``; the abstract
            # BaseCollector.__init__ that type[BaseCollector] exposes does not.
            collector_factory = cast("Callable[..., BaseCollector]", collector_class)
            try:
                # Inside the try: a constructor rejecting its config (e.g. an
                # unparsable timeout) is this source's failure, not the run's
                # (issue #67).
                collector = collector_factory(config=merged_cfg)
                result = collector.collect()
                normalized = normalize_jobs(name, result.jobs)
                source_stats: dict[str, Any] = {
                    "collected": len(normalized),
                    "accepted": 0,
                    "errors": len(result.errors),
                }
                if result.metadata.get("per_query_stats"):
                    source_stats["per_query_stats"] = result.metadata["per_query_stats"]
                if result.metadata.get("per_query_status"):
                    source_stats["per_query_status"] = result.metadata["per_query_status"]
                errors = [f"{name}: {e}" for e in result.errors]
                logger.info("  Collected %d jobs from %s", len(normalized), name)
                return name, normalized, source_stats, errors
            except Exception as exc:
                msg = f"Failed to collect from {name}: {exc}"
                logger.error(msg)
                return name, [], {"collected": 0, "accepted": 0, "errors": 1}, [msg]

        # Run collectors in parallel. Each collector manages its own rate
        # limiting, so concurrent execution is safe. max_workers=6 keeps
        # total concurrency reasonable while letting independent sources
        # (e.g. Lever companies and EURES) overlap their I/O wait.
        # Collection is network-bound and naturally completes one source at a
        # time (as_completed below), so its "item" is a source, not a job —
        # there is no way to know the total job count in advance (that's the
        # point of collecting). stats.collected/by_source are updated here,
        # incrementally, as each source finishes — the exact same aggregate
        # fields the rest of the pipeline already reads, just written earlier
        # than the single end-of-stage assignment that used to be the only
        # update.
        _collect_reporter = _stage_reporter(progress_callback, stats, "collect", len(active_sources))
        _sources_done = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_run_source, cfg): cfg["name"] for cfg in active_sources}
            for future in concurrent.futures.as_completed(futures):
                name, normalized, source_stats, errors = future.result()
                all_jobs.extend(normalized)
                source_stats["status"] = collection_status(source_stats["collected"], source_stats["errors"])
                if source_stats["status"] in ("failed", "partial") and errors:
                    # the Sources table shows a row's reason, as it does for a skipped source
                    source_stats["reason"] = errors[0].removeprefix(f"{name}: ")
                stats.by_source[name] = source_stats
                for err in errors:
                    stats.errors.append(err)
                _sources_done += 1
                stats.collected = len(all_jobs)
                _collect_reporter(_sources_done)

        # Inject pre-collected LinkedIn jobs from the staging snapshot.
        # These arrive as proper Job objects, bypassing the collector executor,
        # so the pipeline never waits for LinkedIn collection.
        if prefetched_linkedin_jobs:
            all_jobs.extend(prefetched_linkedin_jobs)
            stats.by_source["linkedin"] = {
                "collected": len(prefetched_linkedin_jobs),
                "accepted": 0,
                "errors": 0,
                "from_staging": True,
                "status": "completed",
            }
            logger.info(
                "LinkedIn: injected %d pre-collected job(s) from staging snapshot.",
                len(prefetched_linkedin_jobs),
            )

        # Inject pre-collected HelloWork jobs from the staging snapshot, same
        # mechanism as LinkedIn above -- HelloWork's own ~30 minute collection
        # runs on its own daily schedule (src/hellowork/scanner.py), never
        # inside this pipeline run.
        if prefetched_hellowork_jobs:
            all_jobs.extend(prefetched_hellowork_jobs)
            stats.by_source["hellowork"] = {
                "collected": len(prefetched_hellowork_jobs),
                "accepted": 0,
                "errors": 0,
                "from_staging": True,
                "status": "completed",
            }
            logger.info(
                "HelloWork: injected %d pre-collected job(s) from staging snapshot.",
                len(prefetched_hellowork_jobs),
            )

        stats.collected = len(all_jobs)
        stats.stage_times["collect"] = time.perf_counter() - _t_collect
        _notify(progress_callback, "collect", stats)

        # =================================================================
        # VALIDATE
        # =================================================================
        _t_validate = time.perf_counter()
        _validate_reporter = _stage_reporter(progress_callback, stats, "validate", len(all_jobs))
        valid_jobs, validation_errors = validate_jobs(
            all_jobs, progress_callback=lambda p, _t: _validate_reporter(p)
        )
        stats.validated = len(valid_jobs)
        stats.stage_times["validate"] = time.perf_counter() - _t_validate
        for err in validation_errors[:20]:
            stats.errors.append(f"Validation: {err}")
        logger.info("Validation: %d valid, %d errors", len(valid_jobs), len(validation_errors))
        recorder.observe_validation(all_jobs, valid_jobs)

        # Per-source validated counts (rejection funnel: stage 1)
        for job in valid_jobs:
            if job.source in stats.by_source:
                stats.by_source[job.source].setdefault("validated", 0)
                stats.by_source[job.source]["validated"] += 1

        _notify(progress_callback, "validate", stats)

        # =================================================================
        # DEDUPLICATE
        # =================================================================
        _t_dedup = time.perf_counter()
        _dedup_reporter = _stage_reporter(progress_callback, stats, "deduplicate", len(valid_jobs))
        deduped_jobs, duplicates_removed = deduplicate_jobs(
            valid_jobs, progress_callback=lambda p, _t: _dedup_reporter(p)
        )
        stats.deduplicated = len(deduped_jobs)
        stats.stage_times["deduplicate"] = time.perf_counter() - _t_dedup
        logger.info("Deduplication: removed %d duplicates", duplicates_removed)
        recorder.observe_dedup(valid_jobs, deduped_jobs)

        # Per-source unique counts after dedup (rejection funnel: stage 2)
        for job in deduped_jobs:
            if job.source in stats.by_source:
                stats.by_source[job.source].setdefault("unique", 0)
                stats.by_source[job.source]["unique"] += 1

        _notify(progress_callback, "deduplicate", stats)

        # =================================================================
        # FILTER
        # =================================================================
        _t_filter = time.perf_counter()
        rejection_records: list[RejectionRecord] = []
        profile = None
        engine_cfg: dict[str, Any] = {}

        engine_path = active_strategy.path("filters.yaml")

        if profile_path.exists() and engine_path.exists():
            # Reuse the profile already loaded for discovery expansion.
            profile = early_profile if early_profile is not None else load_profile(str(profile_path))
            engine_cfg = load_filters(str(engine_path))  # saved for scoring stage
            filters_config = build_filter_config(profile, engine_cfg)

            # Freshness (ADR 0012): age a re-collected listing from its stored first sighting.
            deduped_jobs = _attach_stored_first_seen(deduped_jobs, storage)
            stats.hard_filter_input = len(deduped_jobs)
            _filter_reporter = _stage_reporter(progress_callback, stats, "filter", len(deduped_jobs))
            filtered_jobs, rejection_reasons = filter_jobs(
                deduped_jobs, filters_config, progress_callback=lambda p, _t: _filter_reporter(p)
            )
            stats.hard_filter_passed = len(filtered_jobs)
            recorder.observe_filter(deduped_jobs, rejection_reasons, filter_config=filters_config)

            deduped_map = {job.id: job for job in deduped_jobs}
            for job_id, reason in rejection_reasons.items():
                filtered_out = deduped_map.get(job_id)
                if filtered_out:
                    rejection_records.append(RejectionRecord(
                        job_id=filtered_out.id,
                        title=filtered_out.title,
                        company=filtered_out.company,
                        location=filtered_out.location,
                        source=filtered_out.source,
                        rejection_stage="filter",
                        rejection_detail=reason,
                    ))

            logger.info(
                "Filtering: %d passed, %d rejected",
                len(filtered_jobs),
                len(rejection_reasons),
            )
        else:
            logger.warning("Profile or filters config not found, skipping filtering")
            filtered_jobs = deduped_jobs
            stats.hard_filter_input = len(deduped_jobs)
            stats.hard_filter_passed = len(filtered_jobs)
            recorder.observe_filter(deduped_jobs, {}, skipped=True)

        # Per-source passed-filter counts and rejection reasons (rejection funnel: stage 3)
        for job in filtered_jobs:
            if job.source in stats.by_source:
                stats.by_source[job.source].setdefault("passed_filter", 0)
                stats.by_source[job.source]["passed_filter"] += 1

        for record in rejection_records:
            src = record.source or "unknown"
            if src in stats.by_source:
                rejections = stats.by_source[src].setdefault("rejection_reasons", {})
                reason = record.rejection_detail or "unknown"
                rejections[reason] = rejections.get(reason, 0) + 1

        stats.filtered = len(filtered_jobs)
        # Default requirements-gate counters to a no-op pass-through so the
        # stats stay internally consistent even if the gate below is skipped
        # (no profile) or fails and falls back to proceeding without it.
        stats.requirements_gate_input = len(filtered_jobs)
        stats.requirements_gate_passed = len(filtered_jobs)
        stats.stage_times["filter"] = time.perf_counter() - _t_filter
        stats.profile_hash = _hash_file(str(profile_path))
        _notify(progress_callback, "filter", stats)

        # =================================================================
        # REQUIREMENTS GATE (hard constraint check: education + principal skills)
        # Runs after the domain/keyword filter and before scoring so that:
        # 1. Bac+5-mandatory jobs are rejected regardless of semantic score.
        # 2. Jobs centered on technologies the user lacks are rejected.
        # 3. Preferred education mismatches inject a score penalty.
        # =================================================================
        _t_gate = time.perf_counter()
        # Populated inside the try block below; kept defined here (empty by
        # default) so the LLM eligibility gate stage that follows can safely
        # read it even when there's no profile or the gate step raised.
        requirements_map: dict[str, Any] = {}
        # Phase LATER / I14: job.id -> GateResult, read by score_jobs_with_breakdown
        # for the soft penalty/experience-years values gate_jobs() computes.
        # Kept defined here (empty by default) for the exact same reason as
        # requirements_map above — scoring must work even with no profile or
        # a raised gate step, just with zero penalties applied.
        gate_results: dict[str, Any] = {}
        if profile is not None:
            try:
                from src.requirements.education import parse_education_level
                from src.requirements.extractor import (
                    RequirementExtractor,
                    build_requirements_llm_provider,
                )
                from src.requirements.gate import (
                    HardConstraintGate,
                    gate_jobs,
                    rejection_reasons_to_detail,
                )

                user_max_edu_level: int | None = None
                if profile.max_education:
                    user_max_edu_level = parse_education_level(profile.max_education)

                # Build LLM provider when enabled in matching config (None otherwise,
                # or if construction fails — the gate always works heuristic-only).
                _gate_llm_provider = build_requirements_llm_provider(profile.matching)

                extractor = RequirementExtractor(
                    user_skills=list(profile.target_skills),
                    llm_provider=_gate_llm_provider,
                    target_domains=list(profile.target_domains),
                )
                user_max_years_exp: int | None = None
                if profile.experience is not None:
                    user_max_years_exp = getattr(profile.experience, "max_years", None)

                gate = HardConstraintGate(
                    user_max_education_level=user_max_edu_level,
                    user_max_years_experience=user_max_years_exp,
                    education_hard_constraint=getattr(profile.matching, "education_hard_constraint", True),
                    principal_skill_gate=getattr(profile.matching, "principal_skill_gate", True),
                    mandatory_skill_gate=getattr(profile.matching, "mandatory_central_skill_gate", True),
                    profile_version_hash=stats.profile_hash,
                )
                edu_penalty = getattr(profile.matching, "education_preferred_penalty", 0.20)

                # Extract requirements for every job that passed the domain filter.
                # This stage has two internal passes over the same job list —
                # extraction (the dominant cost: per-job heuristic/regex
                # parsing, optionally LLM-assisted) then the gate check itself
                # (cheap in-memory boolean evaluation, see gate_jobs() —
                # effectively instantaneous even for thousands of jobs).
                # Reported on one monotonic 0..N scale so "processed" never
                # resets mid-stage, weighted ~90/10 rather than 50/50 so the
                # bar reflects each pass's real share of the wall-clock time
                # instead of jumping the last (fast) half almost instantly.
                stats.requirements_gate_input = len(filtered_jobs)
                _rg_total = len(filtered_jobs)
                _rg_extraction_end = max(1, round(_rg_total * 0.9))
                _rg_reporter = _stage_reporter(progress_callback, stats, "requirements_gate", _rg_total)

                requirements_map = {}
                for _rg_i, job in enumerate(filtered_jobs, start=1):
                    requirements_map[job.id] = extractor.extract(job)
                    _rg_reporter(min(_rg_i, _rg_extraction_end))

                def _rg_gate_progress(
                    processed: int, _total: int,
                    _h: int = _rg_extraction_end, _tot: int = _rg_total,
                ) -> None:
                    _rg_reporter(min(_h + processed, _tot))

                gated_jobs, gate_results = gate_jobs(
                    filtered_jobs, requirements_map, gate,
                    education_preferred_penalty=edu_penalty,
                    progress_callback=_rg_gate_progress,
                )

                # Build rejection records for gate-rejected jobs
                gate_rejected_map = {job.id: job for job in filtered_jobs}
                for job_id, gate_result in gate_results.items():
                    if not gate_result.passes:
                        gated_out = gate_rejected_map.get(job_id)
                        if gated_out:
                            rejection_records.append(RejectionRecord(
                                job_id=gated_out.id,
                                title=gated_out.title,
                                company=gated_out.company,
                                location=gated_out.location,
                                source=gated_out.source,
                                rejection_stage="requirements_gate",
                                rejection_detail=rejection_reasons_to_detail(gate_result.reasons),
                            ))
                            # Update per-source rejection funnel
                            src = gated_out.source or "unknown"
                            if src in stats.by_source:
                                rejections = stats.by_source[src].setdefault("rejection_reasons", {})
                                for gate_reason in gate_result.reasons:
                                    key = f"gate:{gate_reason.type}"
                                    rejections[key] = rejections.get(key, 0) + 1

                gate_rejected_count = len(filtered_jobs) - len(gated_jobs)
                logger.info(
                    "Requirements gate: %d passed, %d rejected",
                    len(gated_jobs), gate_rejected_count,
                )
                # Observed AFTER the stage fully succeeded (journal mirrors what was applied).
                recorder.observe_gate(filtered_jobs, gate_results)
                filtered_jobs = gated_jobs
                stats.filtered = len(filtered_jobs)
                stats.requirements_gate_passed = len(filtered_jobs)

            except Exception as exc:
                logger.warning(
                    "Requirements gate failed — proceeding without it: %s", exc
                )
                stats.requirements_gate_error = str(exc)
                recorder.observe_gate_error(filtered_jobs, str(exc))

        stats.stage_times["requirements_gate"] = time.perf_counter() - _t_gate
        _notify(progress_callback, "requirements_gate", stats)

        # =================================================================
        # LLM SEMANTIC ELIGIBILITY GATE (optional, lightweight)
        # Runs on requirements-gate survivors, before scoring, so the more
        # expensive Phase 2 LLM call further below sees a smaller candidate
        # set. Catches *implicit* education/seniority mismatches the
        # deterministic gate above structurally cannot (see
        # src/ai/eligibility.py's module docstring). Off by default
        # (profile.matching.llm_eligibility_enabled) and only runs when --ai
        # is also active, since it needs a running local LLM server. A
        # failure here never blocks the pipeline — same fallback pattern as
        # the requirements gate above and Phase 2 below.
        # =================================================================
        _t_eligibility = time.perf_counter()
        stats.llm_eligibility_input = len(filtered_jobs)

        from src.ai.eligibility import EligibilityGate

        elig_result = EligibilityGate.run_stage(
            jobs=filtered_jobs,
            requirements_map=requirements_map,
            profile=profile,
            profile_hash=stats.profile_hash,
            db_path=db_path,
            use_ai=use_ai,
        )

        recorder.observe_eligibility(filtered_jobs, elig_result)
        rejection_records.extend(elig_result.rejection_records)
        for record in elig_result.rejection_records:
            src = record.source or "unknown"
            if src in stats.by_source:
                rejections = stats.by_source[src].setdefault("rejection_reasons", {})
                key = "llm_eligibility:role_level"
                rejections[key] = rejections.get(key, 0) + 1

        filtered_jobs = elig_result.survivors
        stats.filtered = len(filtered_jobs)
        stats.llm_eligibility_passed = len(filtered_jobs)
        stats.llm_eligibility_rejected = elig_result.rejected
        stats.llm_eligibility = elig_result.stats

        stats.stage_times["llm_eligibility"] = time.perf_counter() - _t_eligibility
        _notify(progress_callback, "llm_eligibility", stats)

        # =================================================================
        # SCORE
        # =================================================================
        _t_score = time.perf_counter()
        scoring_path = active_strategy.path("scoring.yaml")
        stats.scoring_config_hash = _hash_file(str(scoring_path))

        # Always defined (even when scoring.yaml is missing, see the else
        # branch below) so the PERSONALIZATION stage further down can safely
        # read scoring.domain_relevance.confirmed_wrong_score from it via
        # cap_personalization_at_gates.
        scoring_config: dict[str, Any] = {}
        if scoring_path.exists():
            scoring_config = load_scoring_config(str(scoring_path))
            if profile is not None:
                scoring_config = build_scoring_config(scoring_config, profile, engine_cfg)
            _score_reporter = _stage_reporter(progress_callback, stats, "score", len(filtered_jobs))
            scored_with_breakdown = score_jobs_with_breakdown(
                filtered_jobs, scoring_config, gate_results=gate_results,
                progress_callback=lambda p, _t: _score_reporter(p),
            )
        else:
            logger.warning("Scoring config not found, using default score 0.5")
            scored_with_breakdown = [(job, 0.5, {}) for job in filtered_jobs]

        recorder.observe_gate_results(gate_results)
        recorder.observe_scores(scored_with_breakdown, scoring_config=scoring_config)
        storage_stats = storage.upsert_jobs_batch(
            [job for job, _, _ in scored_with_breakdown]
        )
        logger.info(
            "Storage: %d inserted, %d updated, %d unchanged",
            storage_stats["inserted"],
            storage_stats["updated"],
            storage_stats["unchanged"],
        )

        # Phase NEXT / I11: incremental-collection INFRASTRUCTURE only — record
        # that a source's collection completed cleanly, and how many of its
        # jobs were new vs. already known. Deliberately does NOT change what
        # any collector fetches; see record_source_checkpoint_success's
        # docstring for the correctness contract this must preserve.
        try:
            checkpoint_storage = RunStorage(db_path=db_path)
            checkpoint_storage.init_db()
            try:
                checkpoint_ts = datetime.now(UTC).isoformat()
                by_source_storage: dict[str, dict[str, int]] = storage_stats.get("by_source", {})
                for source_name, source_stat in stats.by_source.items():
                    if not isinstance(source_stat, dict):
                        continue
                    if source_stat.get("errors", 0):
                        continue  # only a clean collection advances the checkpoint
                    if source_stat.get("from_staging"):
                        # Real collection happens on that source's own separate
                        # schedule (LinkedIn/HelloWork scanners), not in this
                        # pipeline run — mirrors source_health.py's own
                        # from_staging exclusion.
                        continue
                    src_storage_stats: dict[str, int] | None = by_source_storage.get(source_name)
                    new_count = src_storage_stats.get("inserted") if src_storage_stats else None
                    known_count = (
                        src_storage_stats.get("updated", 0) + src_storage_stats.get("unchanged", 0)
                        if src_storage_stats
                        else None
                    )
                    checkpoint_storage.record_source_checkpoint_success(
                        source=source_name,
                        run_id=run_id,
                        collected_count=source_stat.get("collected", 0),
                        new_count=new_count,
                        known_count=known_count,
                        timestamp=checkpoint_ts,
                    )
            finally:
                checkpoint_storage.close()
        except Exception as exc:  # noqa: BLE001 - observability only, never break a run
            logger.warning("Source checkpoint recording failed (non-fatal): %s", exc)

        stats.scored = len(scored_with_breakdown)
        stats.stage_times["score+storage"] = time.perf_counter() - _t_score
        _notify(progress_callback, "score", stats)

        # Convert to (job, score) for downstream stages
        scored_jobs = [(job, score) for job, score, _ in scored_with_breakdown]

        # =================================================================
        # PHASE 2: LOCAL AI JOB UNDERSTANDING (optional)
        # Runs between scoring and semantic matching so that AI-extracted
        # skill/seniority/domain text can enrich Phase 3 embeddings.
        # Non-blocking: any failure is logged and the pipeline continues.
        # =================================================================
        enriched_texts: dict[str, str] = {}
        if use_ai:
            _t_phase2 = time.perf_counter()
            try:
                from src.ai.config import load_phase2_config
                from src.ai.interfaces import LocalLLMProvider
                from src.ai.phase3_bridge import build_enriched_texts
                from src.ai.pipeline import Phase2Pipeline
                from src.ai.storage import AIAnalysisStorage

                phase2_cfg = load_phase2_config()
                ai_storage = AIAnalysisStorage(db_path=db_path)
                ai_storage.init_db()

                llm_provider = LocalLLMProvider(
                    endpoint=phase2_cfg.llm_endpoint,
                    model_name=phase2_cfg.llm_model_name,
                    temperature=phase2_cfg.llm_temperature,
                    max_tokens=phase2_cfg.llm_max_tokens,
                    timeout_seconds=phase2_cfg.llm_timeout_seconds,
                    thinking_enabled=phase2_cfg.llm_thinking_enabled,
                    max_workers=phase2_cfg.max_workers,
                )

                # Analyse only the top-N candidates (ordered by deterministic score).
                candidates = [job for job, _ in scored_jobs[: phase2_cfg.max_candidates]]
                profile_dict = profile.model_dump() if profile is not None else None

                # Distinct "phase2" stage — fired immediately, before the
                # (potentially many-minute) batch of LLM calls starts, so the
                # dashboard shows "phase2" rather than appearing stuck on
                # "score" for the whole AI window. Live ticks below keep
                # updating the same stats.phase2 dict as each job finishes;
                # every update goes through the same progress_callback ->
                # RunStorage.update_run_stats path as every other stage, so
                # it survives normal API/frontend polling with no separate
                # in-memory state.
                stats.phase2 = {
                    "total": len(candidates),
                    "completed": 0,
                    "failed": 0,
                    "percent": 0.0,
                    "elapsed_seconds": 0.0,
                }
                # stage_progress unifies phase2 into the same generic
                # {stage, processed, total} shape every other stage uses, so
                # the Dashboard doesn't need a phase2-specific code path —
                # the existing `stats.phase2` dict above is left untouched
                # for the pre-existing frontend/consumer that already reads
                # its richer fields (percent, elapsed_seconds, etc.).
                stats.stage_progress = {"stage": "phase2", "processed": 0, "total": len(candidates)}
                _notify(progress_callback, "phase2", stats)

                def _on_phase2_progress(completed: int, total: int, failed: int) -> None:
                    stats.phase2 = {
                        **stats.phase2,
                        "completed": completed,
                        "failed": failed,
                        "percent": round(100.0 * completed / total, 1) if total else 0.0,
                        "elapsed_seconds": round(time.perf_counter() - _t_phase2, 1),
                    }
                    stats.stage_progress = {"stage": "phase2", "processed": completed, "total": total}
                    _notify(progress_callback, "phase2", stats)

                phase2_pipeline = Phase2Pipeline(
                    storage=storage,
                    ai_storage=ai_storage,
                    llm_provider=llm_provider,
                    prompt_version=phase2_cfg.prompt_version,
                )
                phase2_result = phase2_pipeline.run(
                    candidates,
                    profile=profile_dict,
                    profile_version=stats.profile_hash,
                    on_progress=_on_phase2_progress,
                )

                ai_results_by_job = {r.job_id: r for r in phase2_result.results}
                recorder.observe_phase2(ai_results_by_job)
                enriched_texts = build_enriched_texts(candidates, ai_results_by_job)

                # Success metric for the whole optimization pass: how many
                # jobs actually reach this expensive call. "before" is what
                # Phase 2 would have seen without the eligibility gate
                # upstream (requirements-gate survivors, capped the same way);
                # "after" is what it actually saw (candidates, already
                # eligibility-filtered before this cap was applied above).
                candidates_before_eligibility = min(
                    stats.requirements_gate_passed, phase2_cfg.max_candidates
                )
                candidates_after_eligibility = len(candidates)
                candidates_reduction_pct = (
                    round(
                        100.0 * (1 - candidates_after_eligibility / candidates_before_eligibility), 1
                    )
                    if candidates_before_eligibility
                    else 0.0
                )

                stats.phase2 = {
                    "total": phase2_result.total_jobs,
                    "completed": phase2_result.total_jobs,
                    "successful": phase2_result.successful,
                    "failed": phase2_result.failed,
                    "skipped": phase2_result.skipped,
                    "cached": phase2_result.cached,
                    "retried": phase2_result.retried,
                    "failure_reasons": phase2_result.failure_reasons,
                    "enriched": len(enriched_texts),
                    "percent": 100.0,
                    "time_seconds": time.perf_counter() - _t_phase2,
                    "candidates_before_eligibility": candidates_before_eligibility,
                    "candidates_after_eligibility": candidates_after_eligibility,
                    "candidates_reduction_pct": candidates_reduction_pct,
                }
                logger.info(
                    "Phase 2: %d analyzed, %d enriched texts built",
                    phase2_result.total_jobs,
                    len(enriched_texts),
                )
                ai_storage.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Phase 2 failed — continuing without AI enrichment: %s", exc
                )
                stats.phase2 = {"error": str(exc)}
            stats.stage_times["phase2"] = time.perf_counter() - _t_phase2

        # =================================================================
        # SEMANTIC MATCHING (Phase 3)
        # =================================================================
        sem_config_path = Path(config_dir) / "semantic_config.yaml"
        semantic_config = load_semantic_config(str(sem_config_path))
        stats.model_name = semantic_config.get("embedding", {}).get("model_name", "")

        sem_scores_by_job: dict[str, float] = {}
        final_ranked = scored_jobs

        _t_semantic = time.perf_counter()
        if profile is not None:
            logger.info("Phase 3: Running semantic matching...")
            try:
                embedding_service = EmbeddingService(
                    model_name=semantic_config["embedding"]["model_name"],
                    cache_max_entries=semantic_config["embedding"]["cache_max_entries"],
                    device=semantic_config["embedding"].get("device", "auto"),
                )

                profile_embedding = build_profile_embedding(
                    profile.to_semantic_info(),
                    embedding_service,
                    storage=storage,
                    model_name=embedding_service.cache_model_id,
                )
                jobs_only = [job for job, _ in scored_jobs]

                _sem_timing: dict = {}
                _sem_reporter = _stage_reporter(progress_callback, stats, "semantic", len(jobs_only))
                semantic_results, _ = apply_semantic_matching(
                    jobs_only,
                    profile_embedding,
                    semantic_config,
                    embedding_service,
                    enriched_texts=enriched_texts or None,
                    storage=storage,
                    model_name=embedding_service.cache_model_id,
                    timing=_sem_timing,
                    progress_callback=lambda p, _t: _sem_reporter(p),
                )

                from src.pipeline.semantic_matching import normalize_semantic_score

                sem_scores_by_job = {
                    job.id: normalize_semantic_score(score, semantic_config)
                    for job, score in semantic_results
                }
                recorder.observe_semantic(sem_scores_by_job)
                final_ranked = integrate_semantic_into_ranking(
                    scored_jobs, semantic_results, semantic_config
                )
                stats.semantically_matched = len(semantic_results)
                stats.embedding_cache_hits = int(_sem_timing.get("embed_cache_hits", 0))
                stats.embedding_cache_misses = int(_sem_timing.get("embed_cache_misses", 0))
                for k, v in _sem_timing.items():
                    if isinstance(v, float):
                        stats.stage_times[f"sem.{k}"] = v
                logger.info("Phase 3: complete")
            except Exception as exc:  # noqa: BLE001 - semantic matching only refines the ranking
                # The deterministic scores are already stored: keep their order rather than
                # failing the run (e.g. the embedding model cannot be downloaded or loaded).
                logger.exception("Phase 3: semantic matching failed; keeping the deterministic ranking")
                sem_scores_by_job = {}
                final_ranked = scored_jobs
                stats.semantically_matched = 0
                stats.errors.append(f"Semantic matching skipped: {type(exc).__name__}: {exc}")
        else:
            logger.warning("Profile not loaded — skipping semantic matching")

        stats.stage_times["semantic"] = time.perf_counter() - _t_semantic
        _notify(progress_callback, "semantic", stats)

        # =================================================================
        # PERSONALIZATION (Phase 4)
        # =================================================================
        _t_pers = time.perf_counter()
        pers_storage = PersonalizationStorage(db_path=db_path)
        pers_storage.init_db()
        pers_config = load_personalization_config(str(Path(config_dir) / "personalization.yaml"))

        _pers_timing: dict = {}
        _pers_reporter = _stage_reporter(progress_callback, stats, "personalize", len(final_ranked))
        personalized_ranked, pers_explanations = apply_personalization(
            final_ranked, pers_storage, pers_config, timing=_pers_timing,
            progress_callback=lambda p, _t: _pers_reporter(p),
        )

        # Personalization has no visibility into the domain/seniority
        # dealbreaker gates applied upstream in scoring.py's _apply_weights
        # (it only sees the post-gate composite score) — without this,
        # positive feedback-based personalization can lift a confirmed
        # off-domain or wrong-seniority job back above its gated ceiling,
        # defeating the gate entirely (Pass 5 audit finding). This re-applies
        # the same gate condition post-hoc, from the SAME factors already
        # computed in the SCORE stage above — no duplicated constants, and
        # apply_personalization's own module/contract is untouched.
        factors_by_job_id = {job.id: factors for job, _, factors in scored_with_breakdown}
        personalized_ranked = cap_personalization_at_gates(
            final_ranked, personalized_ranked, factors_by_job_id, scoring_config,
        )

        stats.personalized = len(personalized_ranked)
        stats.personalization_cache_hits = int(_pers_timing.get("pers_cache_hits", 0))
        stats.personalization_cache_misses = int(_pers_timing.get("pers_cache_misses", 0))
        for k, v in _pers_timing.items():
            if isinstance(v, float):
                stats.stage_times[f"pers.{k}"] = v
        stats.stage_times["personalization"] = time.perf_counter() - _t_pers
        logger.info(
            "Phase 4: %d jobs after personalization",
            len(personalized_ranked),
        )
        _notify(progress_callback, "personalize", stats)

        # =================================================================
        # AGGREGATE STATS
        # =================================================================
        all_countries: list[str] = []
        remote_count = 0

        for job, _ in personalized_ranked:
            all_countries.extend(job.countries or [])
            if job.remote:
                remote_count += 1
            if job.source in stats.by_source:
                stats.by_source[job.source]["accepted"] += 1
            else:
                stats.by_source[job.source] = {"collected": 0, "accepted": 1, "errors": 0}

        stats.by_country = dict(Counter(all_countries))
        stats.by_work_mode = {
            "remote": remote_count,
            "on_site": len(personalized_ranked) - remote_count,
        }
        stats.duration_seconds = (datetime.now(UTC) - started_at).total_seconds()

        # =================================================================
        # BUILD SCORED JOB RECORDS
        # =================================================================
        breakdown_by_job = {job.id: bd for job, _, bd in scored_with_breakdown}
        det_score_by_job = {job.id: s for job, s, _ in scored_with_breakdown}

        scored_job_records: list[ScoredJobRecord] = []
        for job, final_score in personalized_ranked:
            exp = pers_explanations.get(job.id)
            scored_job_records.append(ScoredJobRecord(
                job_id=job.id,
                job_title=job.title,
                job_company=job.company,
                job_source=job.source,
                final_score=final_score,
                deterministic_score=det_score_by_job.get(job.id, 0.0),
                semantic_score=sem_scores_by_job.get(job.id, 0.0),
                factors=breakdown_by_job.get(job.id, {}),
                personalization_adjustment=exp.clamped_adjustment if exp else 0.0,
            ))

        recorder.finalize(scored_job_records)
        stats.decisions = recorder.summary()

        pers_storage.close()
        storage.close()

        return PipelineResult(
            run_id=run_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            status="completed",
            stats=stats,
            scored_jobs=scored_job_records,
            rejection_records=rejection_records,
        )

    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        try:
            storage.close()
        except Exception:
            pass
        stats.duration_seconds = (datetime.now(UTC) - started_at).total_seconds()
        return PipelineResult(
            run_id=run_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            status="failed",
            stats=stats,
            scored_jobs=[],
            rejection_records=[],
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def collection_status(collected: int, errors: int) -> str:
    """How one source's collection went: ``completed`` (no error), ``partial`` (errors,
    but jobs came back) or ``failed`` (errors and nothing collected). A source the
    preflight left out is ``skipped`` instead (set where it is skipped)."""
    if not errors:
        return "completed"
    return "partial" if collected else "failed"


def _build_collector_config(source_config: dict[str, Any]) -> dict[str, Any]:
    """Merge top-level source fields with its nested 'config' sub-dict.

    Collectors need fields like careers_url and board_identifier that live
    at the top level of the source YAML entry, plus any extra overrides in
    the nested 'config' dict.  The nested 'config' takes precedence on
    key collisions.
    """
    merged = dict(source_config)
    # `config` may be None when the YAML key exists but has only comments as
    # content (PyYAML parses that as null).  Treat null the same as absent.
    cfg = source_config.get("config") or {}
    merged.update(cfg)
    return merged


def _hash_file(path: str) -> str:
    """Return a short SHA-256 hex digest of a file's content, or '' if missing."""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        return digest[:16]
    except OSError:
        return ""


def _notify(
    callback: ProgressCallback | None,
    stage: str,
    stats: PipelineStats,
) -> None:
    if callback is not None:
        try:
            callback(stage, stats.to_dict())
        except Exception as exc:
            logger.warning("Progress callback error at stage '%s': %s", stage, exc)


def _attach_stored_first_seen(jobs: list[Job], storage: Storage) -> list[Job]:
    """Copy each already-stored job's ``first_seen_at`` onto its freshly collected ``Job``.

    Normalisation builds a new ``Job`` per collection, so without this an undated listing would
    look new on every run (ADR 0012). Storage is unaffected: ``upsert_jobs_batch`` keeps the
    stored value anyway. Fail-open: on a lookup error the jobs are returned unchanged.
    """
    try:
        stored = storage.get_first_seen_at([j.id for j in jobs])
    except Exception as exc:  # noqa: BLE001 — freshness lookup must never fail the run
        logger.warning("first_seen_at lookup failed; undated listings are treated as new: %s", exc)
        return jobs
    if not stored:
        return jobs
    return [j.model_copy(update={"first_seen_at": stored[j.id]}) if j.id in stored else j for j in jobs]


# Item-level progress reporter shared by every stage below. Not a second
# progress mechanism — it only ever forwards through the same `_notify` /
# ProgressCallback channel every stage already used, adding a `stage_progress`
# snapshot to the same `PipelineStats` instance. Purely observational: the
# functions that call the returned reporter (validate_jobs, filter_jobs,
# gate_jobs, etc.) never branch on whether a callback was given except to
# decide whether to call it — the actual filtering/scoring/ranking result is
# identical whether progress_callback is None or not.
def _stage_reporter(
    progress_callback: ProgressCallback | None,
    stats: PipelineStats,
    stage: str,
    total: int,
) -> Callable[[int], None]:
    """Return a callable(processed) that throttles updates to a sane cadence.

    Cadence: report at most ~40 times across the stage (so a 20,000-job
    stage doesn't flood the SSE stream), but never more often than every
    200ms — so a stage that finishes in a few hundred milliseconds still
    gets at least its final update, without back-to-back per-item calls
    for tiny stages. The very last item (processed >= total) always
    reports immediately, uncapped by the time floor, so a stage never
    appears to "hang" at less than 100% after it has actually finished.
    """
    if progress_callback is None or total <= 0:
        return lambda processed: None

    step = max(1, total // 40)
    state = {"last_processed": 0, "last_time": 0.0}

    def _report(processed: int) -> None:
        now = time.perf_counter()
        is_final = processed >= total
        if (
            not is_final
            and processed - state["last_processed"] < step
            and (now - state["last_time"]) < 0.2
        ):
            return
        state["last_processed"] = processed
        state["last_time"] = now
        stats.stage_progress = {"stage": stage, "processed": processed, "total": total}
        _notify(progress_callback, stage, stats)

    return _report


