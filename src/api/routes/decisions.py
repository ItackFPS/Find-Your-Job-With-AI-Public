"""Decision journal API (read-only).

GET /api/jobs/{job_id}/decision               — the latest decision for a job (accepted or rejected)
GET /api/decisions/review-queue               — latest decisions flagged `review`, not yet answered
GET /api/decisions/{decision_id}              — one decision, with its strategy manifest
GET /api/decisions/{decision_id}/snapshot     — the stored job text of a rejected job (if kept)
GET /api/pipeline/runs/{run_id}/decisions     — a run's decisions (filter by outcome / stage)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.decisions.models import DecisionRecord, EvidenceItem
from src.decisions.storage import DecisionStorage

router = APIRouter(prefix="/api", tags=["decisions"])


class DecisionSummary(BaseModel):
    supporting: int
    against: int
    unknown: int
    conflicts: int
    headline: str


class DecisionOut(DecisionRecord):
    """The record plus a ready-to-read summary: what supports it, what argues against, what is
    UNKNOWN, what conflicts."""

    unknown_evidence: list[EvidenceItem] = Field(default_factory=list)
    summary: DecisionSummary


class DecisionPage(BaseModel):
    total: int
    limit: int
    offset: int
    decisions: list[DecisionOut]


class StrategyOut(BaseModel):
    strategy_version: str
    manifest: dict[str, Any] | None


class DecisionDetailOut(DecisionOut):
    strategy: StrategyOut | None = None


def _summary(r: DecisionRecord) -> DecisionSummary:
    supporting = sum(1 for e in r.evidence if e.polarity == "positive")
    against = sum(1 for e in r.evidence if e.polarity == "negative")
    unknown = len(r.unknowns)
    where = f"at {r.deciding_stage}"
    score = f" (score {r.fit_score:.2f})" if r.fit_score is not None else ""
    return DecisionSummary(
        supporting=supporting, against=against, unknown=unknown, conflicts=len(r.conflicts),
        headline=(f"{r.outcome} {where}{score}: {supporting} supporting, {against} against, "
                  f"{unknown} unknown, {len(r.conflicts)} conflict(s) — strategy {r.strategy_version}"))


def _out(r: DecisionRecord) -> DecisionOut:
    return DecisionOut(**r.model_dump(), unknown_evidence=r.unknowns, summary=_summary(r))


def _store(request: Request) -> DecisionStorage:
    store = DecisionStorage(request.app.state.db_path)
    store.init_db()
    return store


@router.get("/jobs/{job_id}/decision", response_model=DecisionOut)
def get_job_decision(job_id: str, request: Request) -> DecisionOut:
    store = _store(request)
    try:
        rec = store.get_latest_decision(job_id)
    finally:
        store.close()
    if rec is None:
        raise HTTPException(status_code=404, detail="No decision recorded for this job")
    return _out(rec)


class ReviewItem(BaseModel):
    decision: DecisionOut
    job_stored: bool          # the job is in `jobs` (accepted at some point): its detail page exists
    has_snapshot: bool        # the job text was kept with the decision (rejected jobs)


class ReviewQueuePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReviewItem]


@router.get("/decisions/review-queue", response_model=ReviewQueuePage)
def review_queue(
    request: Request,
    outcome: str | None = Query(default=None, pattern="^(accepted|rejected|review)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ReviewQueuePage:
    """Jobs whose latest decision was flagged ``review`` and that the user has not answered yet —
    including rejected jobs, so a doubtful rejection is never silent."""
    store = _store(request)
    try:
        total, page = store.review_queue(outcome=outcome, limit=limit, offset=offset)
    finally:
        store.close()
    stored: set[str] = set()
    if page:
        from src.storage.sqlite import Storage

        s = Storage(db_path=request.app.state.db_path)
        s.init_db()
        try:
            stored = set(s.get_jobs_by_ids([r.job_id for r in page]))
        finally:
            s.close()
    return ReviewQueuePage(total=total, limit=limit, offset=offset, items=[
        ReviewItem(decision=_out(r), job_stored=r.job_id in stored, has_snapshot=r.snapshot_hash is not None)
        for r in page])


@router.get("/decisions/{decision_id}", response_model=DecisionDetailOut)
def get_decision(decision_id: str, request: Request) -> DecisionDetailOut:
    store = _store(request)
    try:
        rec = store.get_decision(decision_id)
        manifest = store.get_strategy(rec.strategy_version) if rec else None
    finally:
        store.close()
    if rec is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return DecisionDetailOut(**_out(rec).model_dump(),
                             strategy=StrategyOut(strategy_version=rec.strategy_version, manifest=manifest))


@router.get("/decisions/{decision_id}/snapshot")
def get_decision_snapshot(decision_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    try:
        rec = store.get_decision(decision_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="Decision not found")
        if rec.snapshot_hash is None:
            raise HTTPException(status_code=404, detail="No job text was stored for this decision")
        payload = store.get_snapshot(rec.snapshot_hash)
    finally:
        store.close()
    if payload is None:
        raise HTTPException(status_code=410, detail="The stored job text was purged by the retention policy")
    return payload


@router.get("/pipeline/runs/{run_id}/decisions", response_model=DecisionPage)
def list_run_decisions(
    run_id: str, request: Request,
    outcome: str | None = Query(default=None, pattern="^(accepted|rejected|review)$"),
    needs_review: bool = Query(default=False),
    stage: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> DecisionPage:
    store = _store(request)
    try:
        total, page = store.list_for_run(
            run_id, outcome=outcome, stage=stage, limit=limit, offset=offset, needs_review=needs_review)
    finally:
        store.close()
    return DecisionPage(total=total, limit=limit, offset=offset, decisions=[_out(r) for r in page])
