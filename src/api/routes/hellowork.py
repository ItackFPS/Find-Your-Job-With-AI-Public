"""HelloWork background scanner API routes.

GET  /api/hellowork/status  — current scanner status and waiting job count
POST /api/hellowork/scan    — trigger an immediate scan (409 if already scanning)
POST /api/hellowork/pause   — pause the running scan at its next checkpoint
POST /api/hellowork/resume  — resume a paused/interrupted scan from its checkpoint
GET  /api/hellowork/scans   — scan run history

Mirrors src/api/routes/linkedin.py. "Enabled" here means a 'hellowork' entry
exists in config/sources.yaml (regardless of its `active` flag, which only
gates the normal synchronous per-run collector loop — see
config/sources.yaml's hellowork entry and src/hellowork/scanner.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.scan_control import resume_scan, status_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hellowork", tags=["hellowork"])


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


class HelloWorkStatusOut(BaseModel):
    enabled: bool
    waiting: int
    last_scan: ScanRunOut | None
    is_scanning: bool
    # Pause/resume (issue #81): idle | running | pausing | paused | interrupted,
    # and the durable checkpoint progress (slugs_completed, updated_at; the
    # slug total is not known in advance) — None without a checkpoint.
    state: str = "idle"
    progress: dict[str, Any] | None = None


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

@router.get("/status", response_model=HelloWorkStatusOut)
async def get_status(request: Request) -> HelloWorkStatusOut:
    """Return HelloWork scanner status, waiting job count, and last scan info."""
    scanner = getattr(request.app.state, "hellowork_scanner", None)
    staging = getattr(request.app.state, "hellowork_staging", None)
    if scanner is None or staging is None:
        return HelloWorkStatusOut(enabled=False, waiting=0, last_scan=None, is_scanning=False)

    status = staging.get_status()
    last_scan_row = status.get("last_scan")
    state, progress = status_fields(scanner)

    return HelloWorkStatusOut(
        enabled=True,
        waiting=status["waiting"],
        last_scan=_scan_row_to_out(last_scan_row) if last_scan_row else None,
        is_scanning=scanner.is_scanning(),
        state=state,
        progress=progress,
    )


def _require_scanner(request: Request) -> Any:
    scanner = getattr(request.app.state, "hellowork_scanner", None)
    if scanner is None:
        raise HTTPException(status_code=503, detail="HelloWork scanner is not initialised.")
    return scanner


@router.post("/pause", response_model=HelloWorkStatusOut, status_code=202)
async def pause_scan(request: Request) -> HelloWorkStatusOut:
    """Ask the running scan to pause at its next checkpoint (cooperative).

    Idempotent: pausing when nothing is running (or twice) changes nothing.
    Returns the status; ``state`` is 'pausing' until the scan reaches its
    slug boundary, then 'paused' (which survives restarts).
    """
    scanner = _require_scanner(request)
    if scanner.request_pause():
        logger.info("HelloWork pause requested via API.")
    return await get_status(request)


@router.post("/resume", response_model=ScanResumedOut, status_code=202)
async def resume_scan_route(request: Request) -> ScanResumedOut:
    """Resume from the persisted checkpoint (a normal scan if there is none).

    Returns 409 if a scan is already running or the backend is shutting down.
    """
    scanner = _require_scanner(request)
    try:
        scan_id, exec_id = resume_scan(
            scanner, getattr(request.app.state, "scheduler", None), "hellowork"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("HelloWork scan resumed via API (scan_id=%s, exec_id=%s).", scan_id, exec_id)
    return ScanResumedOut(scan_id=scan_id, exec_id=exec_id)


@router.post("/scan", response_model=ScanStartedOut, status_code=202)
async def trigger_scan(request: Request) -> ScanStartedOut:
    """Trigger an immediate HelloWork scan in the background.

    Returns 409 if a scan is already running.
    Returns 503 if the scanner is not configured (no 'hellowork' entry in
    config/sources.yaml).

    Note: a scan takes roughly 30 minutes (see src/collectors/hellowork.py) —
    this endpoint returns immediately with a scan_id to poll via /status or
    /scans, it does not wait for the scan to finish.
    """
    scanner = getattr(request.app.state, "hellowork_scanner", None)
    if scanner is None:
        raise HTTPException(status_code=503, detail="HelloWork scanner is not initialised.")

    try:
        scan_id = scanner.start_scan()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info("HelloWork scan %s triggered via API.", scan_id)
    return ScanStartedOut(scan_id=scan_id)


@router.get("/scans", response_model=list[ScanRunOut])
async def list_scans(request: Request, limit: int = 20) -> list[ScanRunOut]:
    """Return recent HelloWork scan run history."""
    staging = getattr(request.app.state, "hellowork_staging", None)
    if staging is None:
        return []
    rows = staging.list_scan_runs(limit=min(limit, 100))
    return [_scan_row_to_out(r) for r in rows]
