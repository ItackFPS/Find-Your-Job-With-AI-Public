"""Quality page API: health summary and background quality jobs (plan F9.1).

GET  /api/quality/health            — the latest self-check and evaluation run on disk, as codes + numbers
POST /api/quality/jobs              — start a job {kind: "self_check" | "evaluation", options}; 409 while one runs
GET  /api/quality/jobs              — job history, newest first, and the running job's id
GET  /api/quality/jobs/{id}         — one job: status, phase, result summary or error code
POST /api/quality/jobs/{id}/cancel  — stop the running job

The jobs run the existing evaluation CLI in a child process (``src.api.quality_jobs``); nothing here
evaluates anything itself, and nothing here can approve or promote a strategy (ADR 0011: promotion
stays a human, CLI-only action). Writes go through the same origin and peer guards as every other
mutating route (app middleware).
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.quality_jobs import (
    QualityJobBusy, QualityJobManager, QualityJobStore, latest_evaluation, latest_self_check,
)
from src.api.routes.lab import _runs_dir, _self_check_dir

router = APIRouter(prefix="/api/quality", tags=["quality"])


class QualityJobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # self-check only: also report how much explicit feedback covers the latest decisions (read-only)
    include_feedback: bool = True
    # personal only: which imported calibration set, and which of its labels (P2.4/P2.5)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    split: Literal["heldout", "all"] = "heldout"


class StartQualityJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["self_check", "evaluation", "personal"]
    options: QualityJobOptions = Field(default_factory=QualityJobOptions)


_MANAGER_LOCK = threading.Lock()


def _manager(request: Request) -> QualityJobManager:
    """The app's single job manager (``app.state.quality_jobs``), created on first use."""
    with _MANAGER_LOCK:
        manager: QualityJobManager | None = getattr(request.app.state, "quality_jobs", None)
        if manager is None:
            manager = request.app.state.quality_jobs = QualityJobManager()
        return manager


def _store(request: Request) -> QualityJobStore:
    """The job table, after marking jobs no live worker owns as interrupted."""
    _manager(request).recover(request.app.state.db_path)
    return QualityJobStore(request.app.state.db_path)


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {"self_check": latest_self_check(_self_check_dir(request)),
            "evaluation": latest_evaluation(_runs_dir(request))}


@router.post("/jobs", status_code=202)
def start_job(request: Request, body: StartQualityJobIn = Body(...)) -> dict[str, Any]:
    if body.kind == "personal" and not body.options.dataset_id:
        raise HTTPException(status_code=400, detail={"code": "dataset_required"})
    manager = _manager(request)
    try:
        job_id = manager.start(
            body.kind, body.options.model_dump(), db_path=request.app.state.db_path,
            config_dir=getattr(request.app.state, "config_dir", None) or "config",
            self_check_dir=_self_check_dir(request), runs_dir=_runs_dir(request))
    except QualityJobBusy as exc:
        raise HTTPException(status_code=409, detail={"code": "busy", "active_id": exc.active_id}) from exc
    job = QualityJobStore(request.app.state.db_path).get(job_id)
    return job or {"id": job_id, "kind": body.kind, "status": "running"}


@router.get("/jobs")
def list_jobs(request: Request, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    jobs = _store(request).list(limit)
    return {"active_id": _manager(request).active_id(), "jobs": jobs}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _store(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return job


@router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    if not _manager(request).cancel(job_id):
        job = QualityJobStore(request.app.state.db_path).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        raise HTTPException(status_code=409, detail={"code": "not_running", "status": job["status"]})
    return {"id": job_id, "cancelling": True}
