"""Quality › Calibration API: blind labelling of an exported real dataset (completion plan P2.3).

GET  /api/quality/calibration                    — imported datasets with progress, exports found to import
POST /api/quality/calibration/import             — copy an export folder {source} (the original is only read)
GET  /api/quality/calibration/{id}/next?n=10     — the next offers to label, blind, in the representative order
POST /api/quality/calibration/{id}/answers       — save one answer {job_id, answer, reason?} at once
POST /api/quality/calibration/{id}/save          — write the session's answers as a new dataset version
POST /api/quality/calibration/{id}/extend        — "label 100 more"

Everything happens in ``src.evaluation.calibration``; this module maps its refusals to HTTP codes. It
never reads or writes ``jobs.db``. Writes go through the same origin and peer guards as every other
mutating route (app middleware).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.evaluation import calibration as cal
from src.evaluation.dataset import DatasetError, load_dataset

router = APIRouter(prefix="/api/quality/calibration", tags=["quality"])

_STATUS = {"not_found": 404, "already_imported": 409, "already_labelled": 409}


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=1024)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=128)
    answer: Literal["relevant", "not_relevant", "not_sure"]
    reason: str | None = Field(default=None, max_length=32)   # one of cal.REASONS, checked there


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.db_path).parent


def _root(request: Request) -> Path:
    return Path(getattr(request.app.state, "calibration_dir", None) or cal.calibration_root(_data_dir(request)))


def _refused(exc: cal.CalibrationError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 400), detail={"code": exc.code})


def _exports_found(request: Request, imported: set[str]) -> list[dict[str, Any]]:
    """Real exports under ``<data>/benchmarks/`` not imported yet, so the page can offer them by name."""
    bench = _data_dir(request) / "benchmarks"
    found = []
    if bench.is_dir():
        for child in sorted(p for p in bench.iterdir() if (p / "manifest.json").is_file()):
            try:
                m = load_dataset(child, require_complete_labels=False).manifest
            except DatasetError:
                continue
            if m.kind == "real" and m.dataset_id not in imported:
                found.append({"source": str(child), "dataset_id": m.dataset_id, "total": m.job_count})
    return found


@router.get("")
def overview(request: Request) -> dict[str, Any]:
    datasets = cal.list_datasets(_root(request))
    return {"datasets": datasets, "exports_found": _exports_found(request, {d["dataset_id"] for d in datasets}),
            "reasons": list(cal.REASONS), "max_session": cal.MAX_SESSION}


@router.post("/import")
def import_export(request: Request, body: ImportIn = Body(...)) -> dict[str, Any]:
    try:
        return cal.import_dataset(body.source, _root(request))
    except cal.CalibrationError as exc:
        raise _refused(exc) from exc


@router.get("/{dataset_id}/next")
def next_items(dataset_id: str, request: Request, n: int = Query(default=10, ge=1, le=cal.MAX_SESSION)) -> dict[str, Any]:
    try:
        return {"items": cal.next_items(_root(request), dataset_id, n)}
    except cal.CalibrationError as exc:
        raise _refused(exc) from exc


@router.post("/{dataset_id}/answers")
def answer(dataset_id: str, request: Request, body: AnswerIn = Body(...)) -> dict[str, Any]:
    try:
        return cal.record_answer(_root(request), dataset_id, body.job_id, body.answer, body.reason)
    except cal.CalibrationError as exc:
        raise _refused(exc) from exc


@router.post("/{dataset_id}/save")
def save(dataset_id: str, request: Request) -> dict[str, Any]:
    try:
        return cal.save_session(_root(request), dataset_id)
    except cal.CalibrationError as exc:
        raise _refused(exc) from exc


@router.post("/{dataset_id}/extend")
def extend(dataset_id: str, request: Request) -> dict[str, Any]:
    try:
        return cal.extend_target(_root(request), dataset_id)
    except cal.CalibrationError as exc:
        raise _refused(exc) from exc
