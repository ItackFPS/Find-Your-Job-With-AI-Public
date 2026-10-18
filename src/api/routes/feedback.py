"""Feedback API: explicit feedback (with structured reasons) on a job, append-only.

POST /api/jobs/{id}/feedback        — record explicit feedback
GET  /api/jobs/{id}/feedback        — the job's feedback/behaviour/outcome events, newest first
GET  /api/jobs/feedback/vocabulary  — allowed types and reasons (single source of truth for the UI)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.feedback.models import ALLOWED_REASONS, CANONICAL_STATUSES, EXPLICIT_TYPES, REASONS
from src.feedback.service import FeedbackError, FeedbackService
from src.feedback.storage import FeedbackStorage
from src.storage.sqlite import Storage

router = APIRouter(prefix="/api/jobs", tags=["feedback"])


class FeedbackIn(BaseModel):
    type: str = Field(max_length=40)
    reasons: list[str] = Field(default_factory=list, max_length=12)
    note: str | None = Field(default=None, max_length=500)
    # Provenance: the web UI sends "ui"; anything else is an "api" caller (the default).
    source: Literal["ui", "api"] = "api"
    # The decision the client displayed (validated); default = the job's latest decision.
    decision_id: str | None = Field(default=None, max_length=64)


class FeedbackOut(BaseModel):
    event_id: str
    job_id: str
    decision_id: str | None
    strategy_version: str | None
    signal_class: str
    type: str
    reasons: list[str]
    note: str | None
    source: str
    occurred_at: str
    metadata: dict[str, Any]


class VocabularyOut(BaseModel):
    explicit_types: list[str]
    reasons: list[str]
    allowed_reasons: dict[str, list[str]]
    canonical_statuses: list[str]


def _service(request: Request) -> FeedbackService:
    return FeedbackService(request.app.state.db_path, str(Path(getattr(request.app.state, "config_dir", "config"))))


def _require_job(request: Request, job_id: str) -> None:
    """The job must be known: stored in ``jobs`` or — for a job the pipeline rejected, which is never
    stored there — present in the decision journal (so a doubtful rejection can be answered)."""
    s = Storage(db_path=request.app.state.db_path)
    s.init_db()
    try:
        if s.get_job_by_id(job_id) is not None:
            return
    finally:
        s.close()
    from src.decisions.storage import DecisionStorage

    ds = DecisionStorage(request.app.state.db_path)
    ds.init_db()
    try:
        if ds.get_latest_decision(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
    finally:
        ds.close()


def _out(e: Any) -> FeedbackOut:
    return FeedbackOut(**e.model_dump(include=set(FeedbackOut.model_fields)))


@router.get("/feedback/vocabulary", response_model=VocabularyOut)
def feedback_vocabulary() -> VocabularyOut:
    return VocabularyOut(
        explicit_types=list(EXPLICIT_TYPES), reasons=list(REASONS),
        allowed_reasons={k: sorted(v) for k, v in ALLOWED_REASONS.items()},
        canonical_statuses=list(CANONICAL_STATUSES),
    )


@router.post("/{job_id}/feedback", response_model=FeedbackOut, status_code=201)
def record_feedback(job_id: str, body: FeedbackIn, request: Request) -> FeedbackOut:
    _require_job(request, job_id)
    try:
        event = _service(request).record_explicit(
            job_id, body.type, body.reasons, body.note, source=body.source, decision_id=body.decision_id)
    except FeedbackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(event)


@router.get("/{job_id}/feedback", response_model=list[FeedbackOut])
def list_feedback(
    job_id: str, request: Request, limit: int = Query(default=50, ge=1, le=200),
) -> list[FeedbackOut]:
    _require_job(request, job_id)
    store = FeedbackStorage(request.app.state.db_path)
    try:
        store.init_db()
        return [_out(e) for e in store.list_for_job(job_id, limit=limit)]
    finally:
        store.close()
