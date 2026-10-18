"""Pipeline run API routes.

POST /api/pipeline/run                    — trigger a background run
GET  /api/pipeline/runs                   — list recent runs
GET  /api/pipeline/runs/{id}              — run status + stats
GET  /api/pipeline/runs/{id}/jobs         — job scores for a specific run
GET  /api/pipeline/runs/{id}/jobs/n8n     — lightweight job listing for n8n (score_100 + tier)
GET  /api/pipeline/events                 — SSE stream of live run progress (observational only)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.auth import require_n8n_api_key
from pydantic import BaseModel

from src.api.runner import PipelineRunner
from src.api.schemas import (
    JobOut,
    PipelineEventOut,
    PipelineRunOut,
    RunStats,
    ScoreBreakdownOut,
    StartRunIn,
    StartRunOut,
)
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

if TYPE_CHECKING:
    from src.api.progress_stream import ProgressBus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

# How long the SSE loop waits for a queued event before sending a keep-alive
# comment. Also bounds how quickly a client disconnect is noticed.
_SSE_HEARTBEAT_SECONDS = 15.0


def _get_runner(request: Request) -> PipelineRunner:
    runner: PipelineRunner = request.app.state.runner
    return runner


def _get_run_storage(request: Request) -> RunStorage:
    rs = RunStorage(db_path=request.app.state.db_path)
    rs.init_db()
    return rs


def _get_job_storage(request: Request) -> Storage:
    s = Storage(db_path=request.app.state.db_path)
    s.init_db()
    return s


@router.post("/run", response_model=StartRunOut, status_code=202)
def start_run(
    request: Request,
    body: StartRunIn | None = Body(default=None),
) -> StartRunOut:
    """Trigger a background pipeline run.

    ``body`` is optional — an empty request (no body, or ``{}``) preserves
    the pre-existing default of AI disabled. Pass ``{"use_ai": true}`` to
    run with Phase 2 (local AI) enabled, the same flag the CLI's ``--ai``
    switch controls.
    """
    runner = _get_runner(request)
    use_ai = body.use_ai if body is not None else False
    try:
        run_id = runner.start_run(use_ai=use_ai)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StartRunOut(run_id=run_id)


@router.get("/runs", response_model=list[PipelineRunOut])
def list_runs(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> list[PipelineRunOut]:
    rs = _get_run_storage(request)
    try:
        rows = rs.list_runs(limit=limit)
        return [_row_to_run_out(r) for r in rows]
    finally:
        rs.close()


@router.get("/runs/{run_id}", response_model=PipelineRunOut)
def get_run(run_id: str, request: Request) -> PipelineRunOut:
    rs = _get_run_storage(request)
    try:
        row = rs.get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _row_to_run_out(row)
    finally:
        rs.close()


def _format_sse(event: dict[str, Any]) -> str:
    """Validate a raw event dict through PipelineEventOut and format as SSE.

    Routing every event through the Pydantic model guarantees the stream can
    never carry more than the polling endpoint already exposes (no job
    descriptions, no secrets, no stack traces) — see PipelineEventOut.
    """
    validated = PipelineEventOut(
        type=event.get("type", "pipeline.progress"),
        run_id=event.get("run_id", ""),
        status=event.get("status", ""),
        stage=event.get("stage", ""),
        stats=_build_run_stats(event.get("stats") or {}),
        timestamp=event.get("timestamp", ""),
        error=event.get("error"),
    )
    return f"event: {validated.type}\ndata: {validated.model_dump_json()}\n\n"


def _build_snapshot_event(db_path: str, runner: PipelineRunner) -> dict[str, Any] | None:
    """Authoritative current-state snapshot for a newly connected client.

    Reads straight from RunStorage/PipelineRunner (the same source GET
    /runs/{id} and GET /dashboard already use) rather than caching state in
    the bus, so a Dashboard opened mid-run — or after a backend restart —
    sees the real state instead of a stale in-memory guess.
    """
    rs = RunStorage(db_path=db_path)
    rs.init_db()
    try:
        run_id = runner.current_run_id()
        row = rs.get_run(run_id) if run_id is not None else rs.get_latest_completed_run()
        if row is None:
            return None
        stats_raw = row.get("stats") or {}
        return {
            "type": "pipeline.snapshot",
            "run_id": row["id"],
            "status": row["status"],
            "stage": stats_raw.get("current_stage", ""),
            "stats": stats_raw,
            "error": row.get("error"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        rs.close()


@router.get("/events")
async def stream_pipeline_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of live pipeline progress.

    Purely observational: mirrors the same RunStats already returned by
    GET /runs/{id}, and never decides whether a job passes, is scored, or
    is notified. On connect, emits one snapshot event built from the
    authoritative DB/runner state (so a newly opened Dashboard doesn't
    start from zero), then streams live events as PipelineRunner publishes
    them. If the browser disconnects, refreshes, or never connects at all,
    the pipeline itself is entirely unaffected — this endpoint only reads
    from a best-effort in-process fan-out (src/api/progress_stream.py).
    """
    bus: "ProgressBus" = request.app.state.progress_bus
    runner = _get_runner(request)
    db_path = request.app.state.db_path

    async def event_source() -> AsyncIterator[str]:
        queue = bus.subscribe()
        try:
            try:
                snapshot = _build_snapshot_event(db_path, runner)
            except Exception as exc:  # noqa: BLE001
                logger.warning("SSE snapshot build failed (non-fatal): %s", exc)
                snapshot = None
            if snapshot is not None:
                yield _format_sse(snapshot)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _format_sse(event)
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/notifications")
def get_notification_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """Return recent notification history (sent/failed records)."""
    engine = getattr(request.app.state, "notification_engine", None)
    if engine is None:
        return []
    history: list[dict] = engine.get_history(limit=limit)
    return history


@router.get("/notifications/status")
def get_notification_status(request: Request) -> dict:
    """Return provider enabled/configured status and recent counts."""
    engine = getattr(request.app.state, "notification_engine", None)
    if engine is None:
        status: dict = {"enabled": False, "providers": {}, "counts": {}}
    else:
        status = engine.get_provider_status()
    status["discord_server_url"] = discord_server_url()
    return status


def discord_server_url() -> str | None:
    """Link to the user's own Discord server, built from DISCORD_GUILD_ID.

    None when unset or not a numeric guild ID, so the UI shows no link rather
    than a hard-coded server (the frontend used to embed the owner's).
    """
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not guild_id.isdigit():
        return None
    return f"https://discord.com/channels/{guild_id}"


@router.get("/runs/{run_id}/jobs", response_model=list[JobOut])
def get_run_jobs(
    run_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[JobOut]:
    rs = _get_run_storage(request)
    job_storage = _get_job_storage(request)
    try:
        score_rows = rs.get_jobs_for_run(run_id, limit=limit, offset=offset)
        jobs_by_id = job_storage.get_jobs_by_ids([sr["job_id"] for sr in score_rows])
        result: list[JobOut] = []
        for sr in score_rows:
            job = jobs_by_id.get(sr["job_id"])
            if job is None:
                continue
            result.append(_job_to_out(job, sr))
        return result
    finally:
        rs.close()
        job_storage.close()


# ---------------------------------------------------------------------------
# n8n lightweight endpoint
# ---------------------------------------------------------------------------

class N8nJobOut(BaseModel):
    """Lightweight job response optimised for n8n automation workflows.

    ``score_100`` is final_score * 100 rounded to the nearest integer.
    ``tier`` classifies the job for routing (Phase 4, 2026-09-10
    fixed-reference calibration — see src/pipeline/scoring.py's
    _TIER_THRESHOLDS for the calibration methodology):
        exceptional  — score_100 >= 80  → immediate Discord notification
        excellent    — score_100 >= 72  → priority Discord notification
        strong       — score_100 >= 65  → normal notification / digest
        relevant     — score_100 >= 60  → daily/periodic digest
        low_priority — score_100  < 60  → analytics only, no notification
    """

    job_id: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    countries: list[str] = []
    remote: bool = False
    work_mode: str | None = None
    employment_type: str | None = None
    skills: list[str] = []
    source: str = ""
    final_score: float = 0.0
    score_100: int = 0
    tier: str = "low_priority"
    canonical_url: str | None = None
    apply_url: str | None = None
    published_at: datetime | None = None


@router.get(
    "/runs/{run_id}/jobs/n8n",
    response_model=list[N8nJobOut],
    dependencies=[Depends(require_n8n_api_key)],
)
def get_run_jobs_n8n(
    run_id: str,
    request: Request,
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[N8nJobOut]:
    """Lightweight paginated job listing for n8n automation.

    Returns all jobs scored in a run, sorted by final_score descending.
    Each item includes ``score_100`` (0-100 integer) and ``tier`` so that
    n8n can route without recalculating scores.

    Paginate with ``limit`` / ``offset``.  When the returned array is shorter
    than ``limit``, there are no more pages.

    Intended workflow:
        1. Receive ``pipeline.completed`` webhook (contains run_id + all_jobs_count).
        2. Loop: GET /api/pipeline/runs/{run_id}/jobs/n8n?limit=500&offset=N
        3. Route jobs by ``tier``.
        4. Stop when page length < limit.
    """
    rs = _get_run_storage(request)
    job_storage = _get_job_storage(request)
    try:
        run = rs.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")

        score_rows = rs.get_jobs_for_run(run_id, limit=limit, offset=offset)
        if not score_rows:
            return []

        # Batch-fetch job details to avoid N individual DB reads.
        job_ids = [sr["job_id"] for sr in score_rows]
        from src.api.routes.jobs import _get_jobs_filtered
        # Deferred: src.pipeline's package __init__ eagerly imports
        # sentence-transformers/torch — see src/notifications/events.py's
        # JobSummary.to_dict for the same concern.
        from src.pipeline.scoring import score_to_tier
        jobs_list = _get_jobs_filtered(job_storage, job_ids=job_ids)
        jobs_by_id = {j.id: j for j in jobs_list}

        result: list[N8nJobOut] = []
        for sr in score_rows:
            jid = sr["job_id"]
            job = jobs_by_id.get(jid)
            if job is None:
                continue
            final_score = round(float(sr.get("final_score") or 0.0), 4)
            score_100 = round(final_score * 100)
            result.append(N8nJobOut(
                job_id=jid,
                title=job.title,
                company=job.company,
                location=job.location,
                countries=job.countries or [],
                remote=job.remote,
                work_mode=job.work_mode,
                employment_type=job.employment_type,
                skills=(job.skills or [])[:5],
                source=job.source,
                final_score=final_score,
                score_100=score_100,
                tier=score_to_tier(score_100),
                canonical_url=job.canonical_url,
                apply_url=job.apply_url,
                published_at=job.published_at,
            ))
        return result
    finally:
        rs.close()
        job_storage.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_run_stats(stats_raw: dict) -> RunStats:
    """Build a ``RunStats`` from a raw stats dict (DB row or live event).

    Shared by ``_row_to_run_out`` (GET /runs/{id}) and the SSE stream
    (GET /events) so both surfaces expose exactly the same fields — this
    also acts as a whitelist: any key not declared on ``RunStats`` is
    silently dropped rather than reaching the client.
    """
    by_source_raw = stats_raw.get("by_source", {})

    from src.api.schemas import SourceStats
    by_source = {k: SourceStats(**v) if isinstance(v, dict) else v for k, v in by_source_raw.items()}

    return RunStats(
        collected=stats_raw.get("collected", 0),
        validated=stats_raw.get("validated", 0),
        deduplicated=stats_raw.get("deduplicated", 0),
        filtered=stats_raw.get("filtered", 0),
        hard_filter_input=stats_raw.get("hard_filter_input", 0),
        hard_filter_passed=stats_raw.get("hard_filter_passed", 0),
        hard_filter_rejected=stats_raw.get("hard_filter_rejected", 0),
        requirements_gate_input=stats_raw.get("requirements_gate_input", 0),
        requirements_gate_passed=stats_raw.get("requirements_gate_passed", 0),
        requirements_gate_rejected=stats_raw.get("requirements_gate_rejected", 0),
        requirements_gate_error=stats_raw.get("requirements_gate_error"),
        llm_eligibility_input=stats_raw.get("llm_eligibility_input", 0),
        llm_eligibility_passed=stats_raw.get("llm_eligibility_passed", 0),
        llm_eligibility_rejected=stats_raw.get("llm_eligibility_rejected", 0),
        llm_eligibility=stats_raw.get("llm_eligibility", {}),
        validation_rejected=stats_raw.get("validation_rejected", 0),
        deduplication_removed=stats_raw.get("deduplication_removed", 0),
        filter_rejected=stats_raw.get("filter_rejected", 0),
        scored=stats_raw.get("scored", 0),
        semantically_matched=stats_raw.get("semantically_matched", 0),
        personalized=stats_raw.get("personalized", 0),
        duration_seconds=stats_raw.get("duration_seconds", 0.0),
        by_source=by_source,
        by_country=stats_raw.get("by_country", {}),
        by_work_mode=stats_raw.get("by_work_mode", {}),
        errors=stats_raw.get("errors", []),
        profile_hash=stats_raw.get("profile_hash", ""),
        model_name=stats_raw.get("model_name", ""),
        scoring_config_hash=stats_raw.get("scoring_config_hash", ""),
        current_stage=stats_raw.get("current_stage", ""),
        use_ai=stats_raw.get("use_ai", False),
        phase2=stats_raw.get("phase2", {}),
        stage_progress=stats_raw.get("stage_progress", {}),
    )


def _row_to_run_out(row: dict) -> PipelineRunOut:
    stats_raw = row.get("stats", {})
    return PipelineRunOut(
        id=row["id"],
        started_at=row["started_at"],
        completed_at=row.get("completed_at"),
        status=row["status"],
        stats=_build_run_stats(stats_raw),
        error=row.get("error"),
    )


def _build_score_breakdown_out(score_row: dict | None) -> ScoreBreakdownOut | None:
    """Build a ``ScoreBreakdownOut`` from a stored score row, including the
    canonical tier (Phase 5) — the single place both ``_job_to_out`` and
    ``jobs.py``'s ``_job_to_job_detail_out`` get score_100/tier from, so
    the two never drift out of sync with each other or with
    ``score_to_tier()``.
    """
    if not score_row:
        return None
    from src.pipeline.scoring import score_to_tier

    final_score = score_row.get("final_score")
    score_100 = round(float(final_score) * 100) if final_score is not None else None
    return ScoreBreakdownOut(
        deterministic_score=score_row.get("deterministic_score"),
        semantic_score=score_row.get("semantic_score"),
        final_score=final_score,
        factors=score_row.get("score_breakdown", {}),
        run_id=score_row.get("run_id"),
        score_100=score_100,
        tier=score_to_tier(score_100) if score_100 is not None else None,
    )


def _resolve_department(j: "object") -> str | None:
    """Resolve a job's French department code the same way
    ``src.pipeline.location_match.resolve_job_location`` already does:
    prefer the canonical ``department`` column, else fall back to parsing
    the location string. One shared implementation so the frontend needs no
    copy of this logic.
    """
    dept: str | None = getattr(j, "department", None)
    if dept:
        return dept
    from src.pipeline.departments import extract_department_code

    location = getattr(j, "location", None) or ""
    countries = getattr(j, "countries", None) or []
    return extract_department_code(location, countries) or None


def _job_to_out(job: object, score_row: dict | None = None) -> JobOut:
    from src.models.job import Job as JobModel
    j: JobModel = job  # type: ignore[assignment]

    score_out = _build_score_breakdown_out(score_row)

    return JobOut(
        id=j.id,
        source=j.source,
        title=j.title,
        company=j.company,
        location=j.location,
        department=_resolve_department(j),
        countries=j.countries,
        remote=j.remote,
        work_mode=j.work_mode,
        employment_type=j.employment_type,
        salary_min=j.salary_min,
        salary_max=j.salary_max,
        salary_currency=j.salary_currency,
        published_at=j.published_at,
        discovered_at=j.discovered_at,
        skills=j.skills,
        canonical_url=j.canonical_url,
        apply_url=j.apply_url,
        status=j.status,
        application_status=j.application_status,
        score=score_out,
    )
