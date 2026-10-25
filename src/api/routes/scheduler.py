"""Scheduler status and control API routes.

GET  /api/scheduler/status          — current status of all schedules (+ source breakers)
GET  /api/scheduler/executions      — recent execution history
POST /api/scheduler/run/{id}        — manually trigger a schedule
POST /api/scheduler/pause           — pause scheduled runs and running scans (the tray's "Pause scans")
POST /api/scheduler/resume          — resume them
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status")
def get_scheduler_status(request: Request) -> dict[str, Any]:
    """Return the current status of the scheduler and all defined schedules."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        status: dict[str, Any] = {"schedules": [], "note": "Scheduler not initialized"}
    else:
        status = scheduler.get_status()
    # Per-source circuit breakers (plan F6.1), for Settings' source health.
    from src.api.routes.sources import breaker_rows

    status["source_breakers"] = breaker_rows(request.app.state.db_path)
    return status


@router.get("/executions")
def list_executions(
    request: Request,
    definition_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List recent scheduler execution records."""
    from src.scheduler.storage import SchedulerStorage

    db_path = request.app.state.db_path
    storage = SchedulerStorage(db_path=db_path)
    try:
        return storage.list_executions(definition_id=definition_id, limit=limit)
    finally:
        storage.close()


@router.post("/pause")
def pause_scheduler(request: Request) -> dict[str, Any]:
    """Stop scheduled dispatches; running scans stop at their next checkpoint."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not available")
    return {"paused": True, "pausing": scheduler.pause_all()}


@router.post("/resume")
def resume_scheduler(request: Request) -> dict[str, Any]:
    """Dispatch scheduled runs again; paused scans resume from their checkpoint."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not available")
    return {"paused": False, "resumed": scheduler.resume_all()}


@router.post("/run/{definition_id}")
def trigger_manual_run(definition_id: str, request: Request) -> dict[str, Any]:
    """Manually trigger a scheduled job, bypassing the startup policy.

    Returns 409 if the source is already running.
    Returns 404 if the definition_id is not found.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not available")
    try:
        exec_id = scheduler.trigger_manual(definition_id)
        return {"exec_id": exec_id, "status": "started"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
