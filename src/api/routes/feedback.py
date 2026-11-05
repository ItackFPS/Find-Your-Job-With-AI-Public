"""Feedback API: explicit feedback (with structured reasons) on a job, append-only.

POST /api/jobs/{id}/feedback        — record explicit feedback, or a list action (saved, unsaved,
                                      hidden, unhidden; ADR 0016/0017)
GET  /api/jobs/feedback/list-state  — the offers the user currently saves or hides
GET  /api/jobs/{id}/feedback        — the job's feedback/behaviour/outcome events, newest first
GET  /api/jobs/feedback/vocabulary  — allowed types and reasons (single source of truth for the UI)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.feedback.exposure import MAX_BATCH, ExposureItem, ExposureStorage
from src.feedback.models import ALLOWED_REASONS, CANONICAL_STATUSES, EXPLICIT_TYPES, LIST_TYPES, REASONS
from src.feedback.service import Delivery, FeedbackError, FeedbackService
from src.feedback.storage import FeedbackStorage
from src.storage.app_state import GenerationMismatch
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
    # Delivery from the client outbox (ADR 0016). Optional; when ``generation`` is sent it must be
    # the current ``<data_epoch>:<generation>`` or the write is refused with 409.
    client_event_id: str | None = Field(default=None, min_length=8, max_length=64)
    generation: str | None = Field(default=None, max_length=80)
    client_instance_id: str | None = Field(default=None, max_length=64)
    client_seq: int | None = Field(default=None, ge=0)
    client_created_at: str | None = Field(default=None, max_length=40)
    context: dict[str, Any] | None = None


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
    client_event_id: str | None = None
    generation: int | None = None
    superseded: bool = False


class ExposureItemIn(BaseModel):
    job_id: str = Field(max_length=200)
    surface: str = Field(max_length=40)
    position: int | None = Field(default=None, ge=0)
    visible_ms: int | None = Field(default=None, ge=0)
    sort_key: str | None = Field(default=None, max_length=40)
    randomized: bool = False
    client_created_at: str | None = Field(default=None, max_length=40)


class ExposureBatchIn(BaseModel):
    generation: str | None = Field(default=None, max_length=80)
    session_id: str | None = Field(default=None, max_length=64)
    items: list[ExposureItemIn] = Field(default_factory=list, max_length=MAX_BATCH)


_RETENTION_DONE: dict[str, str] = {}


@router.post("/feedback/exposure")
def record_exposure(body: ExposureBatchIn, request: Request) -> dict[str, int]:
    """Best-effort batch of visible exposures (ADR 0016). A missing or stale generation drops the
    batch silently ("stored": 0); retention runs at most once a day per database."""
    from datetime import UTC, datetime

    db = request.app.state.db_path
    store = ExposureStorage(db)
    try:
        store.init_db()
        stored = store.record_batch(body.generation, body.session_id, [
            ExposureItem(**i.model_dump()) for i in body.items])
        today = datetime.now(UTC).date().isoformat()
        if _RETENTION_DONE.get(db) != today:
            store.apply_retention()
            _RETENTION_DONE[db] = today
    finally:
        store.close()
    return {"stored": stored}


class ListStateOut(BaseModel):
    saved: list[str]
    hidden: list[str]


class VocabularyOut(BaseModel):
    explicit_types: list[str]
    list_types: list[str] = Field(default_factory=list)
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
        explicit_types=list(EXPLICIT_TYPES), list_types=list(LIST_TYPES), reasons=list(REASONS),
        allowed_reasons={k: sorted(v) for k, v in ALLOWED_REASONS.items()},
        canonical_statuses=list(CANONICAL_STATUSES),
    )


@router.get("/feedback/list-state", response_model=ListStateOut)
def list_state(request: Request) -> ListStateOut:
    store = FeedbackStorage(request.app.state.db_path)
    try:
        store.init_db()
        state = store.list_state()
    finally:
        store.close()
    return ListStateOut(saved=sorted(j for j, s in state.items() if s["saved"]),
                        hidden=sorted(j for j, s in state.items() if s["hidden"]))


@router.post("/{job_id}/feedback", response_model=FeedbackOut, status_code=201)
def record_feedback(job_id: str, body: FeedbackIn, request: Request) -> FeedbackOut:
    _require_job(request, job_id)
    delivery = Delivery(
        client_event_id=body.client_event_id, generation_token=body.generation,
        client_instance_id=body.client_instance_id, client_seq=body.client_seq,
        client_created_at=body.client_created_at, context=body.context)
    service = _service(request)
    try:
        if body.type in LIST_TYPES:
            event = service.record_list(job_id, body.type, body.reasons, source=body.source, delivery=delivery)
        else:
            event = service.record_explicit(
                job_id, body.type, body.reasons, body.note, source=body.source,
                decision_id=body.decision_id, delivery=delivery)
    except FeedbackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GenerationMismatch as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
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
