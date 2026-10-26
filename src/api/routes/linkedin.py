"""LinkedIn background scanner API routes.

GET  /api/linkedin/status  — current scanner status and waiting job count
POST /api/linkedin/scan    — trigger an immediate scan (409 if already scanning)
POST /api/linkedin/pause   — pause the running scan at its next checkpoint
POST /api/linkedin/resume  — resume a paused/interrupted scan from its checkpoint
GET  /api/linkedin/scans   — scan run history
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.scan_control import resume_scan, status_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class ScanRunOut(BaseModel):
    id: str
    started_at: str
    completed_at: str | None
    status: str
    jobs_found: int
    jobs_new: int
    error: str | None
    stats: dict[str, Any] | None = None


class LinkedInStatusOut(BaseModel):
    enabled: bool
    waiting: int
    last_scan: ScanRunOut | None
    is_scanning: bool
    # Pause/resume (issue #81): idle | running | pausing | paused | interrupted,
    # and the durable checkpoint progress (queries_completed, queries_total,
    # current_query, current_page, updated_at) — None when idle without a
    # checkpoint. Issue #86 adds the work-based estimate: percent,
    # pages_done_est, pages_total_est, current_query_pages_est, estimated,
    # eta_seconds and eta_basis (live|checkpoint|history; both None unless
    # actively running) — see src/linkedin/scan_progress.py.
    state: str = "idle"
    progress: dict[str, Any] | None = None
    # A scan stopped on a 429 (state "rate_limited", F6.4): when it resumes on its own.
    resume_after: str | None = None


class ScanStartedOut(BaseModel):
    scan_id: str


class ScanResumedOut(BaseModel):
    """scan_id when started directly, exec_id when resumed through the schedule."""
    scan_id: str | None = None
    exec_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scan_row_to_out(row: dict[str, Any]) -> ScanRunOut:
    stats: dict[str, Any] | None = None
    raw_stats = row.get("stats_json")
    if raw_stats and raw_stats != "{}":
        try:
            stats = json.loads(raw_stats)
        except (json.JSONDecodeError, TypeError):
            pass
    return ScanRunOut(
        id=row["id"],
        started_at=row["started_at"],
        completed_at=row.get("completed_at"),
        status=row["status"],
        jobs_found=row.get("jobs_found") or 0,
        jobs_new=row.get("jobs_new") or 0,
        error=row.get("error"),
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status", response_model=LinkedInStatusOut)
async def get_status(request: Request) -> LinkedInStatusOut:
    """Return LinkedIn scanner status, waiting job count, and last scan info."""
    return _build_status(request)


def _build_status(request: Request) -> LinkedInStatusOut:
    config = getattr(request.app.state, "linkedin_config", None)
    if config is None or not config.enabled:
        return LinkedInStatusOut(
            enabled=False,
            waiting=0,
            last_scan=None,
            is_scanning=False,
        )

    staging = request.app.state.linkedin_staging
    scanner = getattr(request.app.state, "linkedin_scanner", None)

    status = staging.get_status()
    is_scanning = scanner.is_scanning() if scanner else False
    last_scan_row = status.get("last_scan")
    state, progress = status_fields(scanner) if scanner else ("idle", None)

    return LinkedInStatusOut(
        enabled=True,
        waiting=status["waiting"],
        last_scan=_scan_row_to_out(last_scan_row) if last_scan_row else None,
        is_scanning=is_scanning,
        state=state,
        progress=progress,
        resume_after=_resume_after(scanner),
    )


def _require_scanner(request: Request) -> Any:
    config = getattr(request.app.state, "linkedin_config", None)
    if config is None or not config.enabled:
        raise HTTPException(
            status_code=503,
            detail="LinkedIn scanner is disabled. Set enabled: true in config/linkedin.yaml.",
        )
    scanner = getattr(request.app.state, "linkedin_scanner", None)
    if scanner is None:
        raise HTTPException(status_code=503, detail="LinkedIn scanner is not initialised.")
    return scanner


@router.post("/pause", response_model=LinkedInStatusOut, status_code=202)
async def pause_scan(request: Request) -> LinkedInStatusOut:
    """Ask the running scan to pause at its next checkpoint (cooperative).

    Idempotent: pausing when nothing is running (or twice) changes nothing.
    Returns the status; ``state`` is 'pausing' until the scan reaches its
    checkpoint boundary, then 'paused' (which survives restarts).
    """
    scanner = _require_scanner(request)
    if scanner.request_pause():
        logger.info("LinkedIn pause requested via API.")
    return _build_status(request)


@router.post("/resume", response_model=ScanResumedOut, status_code=202)
async def resume_scan_route(request: Request) -> ScanResumedOut:
    """Resume from the persisted checkpoint (a normal scan if there is none).

    Returns 409 if a scan is already running or the backend is shutting down.
    """
    scanner = _require_scanner(request)
    try:
        scan_id, exec_id = resume_scan(
            scanner, getattr(request.app.state, "scheduler", None), "linkedin"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("LinkedIn scan resumed via API (scan_id=%s, exec_id=%s).", scan_id, exec_id)
    return ScanResumedOut(scan_id=scan_id, exec_id=exec_id)


@router.post("/scan", response_model=ScanStartedOut, status_code=202)
async def trigger_scan(request: Request) -> ScanStartedOut:
    """Trigger an immediate LinkedIn scan in the background.

    Returns 409 if a scan is already running.
    Returns 503 if LinkedIn is disabled or the scanner is not configured.
    """
    config = getattr(request.app.state, "linkedin_config", None)
    if config is None or not config.enabled:
        raise HTTPException(
            status_code=503,
            detail="LinkedIn scanner is disabled. Set enabled: true in config/linkedin.yaml.",
        )

    scanner = getattr(request.app.state, "linkedin_scanner", None)
    if scanner is None:
        raise HTTPException(status_code=503, detail="LinkedIn scanner is not initialised.")

    try:
        scan_id = scanner.start_scan()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info("LinkedIn scan %s triggered via API.", scan_id)
    return ScanStartedOut(scan_id=scan_id)


@router.get("/scans", response_model=list[ScanRunOut])
async def list_scans(request: Request, limit: int = 20) -> list[ScanRunOut]:
    """Return recent LinkedIn scan run history."""
    config = getattr(request.app.state, "linkedin_config", None)
    if config is None or not config.enabled:
        return []

    staging = request.app.state.linkedin_staging
    rows = staging.list_scan_runs(limit=min(limit, 100))
    return [_scan_row_to_out(r) for r in rows]


def _resume_after(scanner: Any) -> str | None:
    """ISO time a rate-limited scan resumes by itself (F6.4), else None."""
    fn = getattr(scanner, "rate_limited_until", None)
    value = fn() if callable(fn) else None
    return value if isinstance(value, str) else None
