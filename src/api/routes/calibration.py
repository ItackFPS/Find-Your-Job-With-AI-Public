"""Quality › Calibration API: blind labelling of an exported real dataset (completion plan P2.3).

GET  /api/quality/calibration                    — imported datasets with progress, exports found to import
POST /api/quality/calibration/import             — copy an export folder {source} (the original is only read)
GET  /api/quality/calibration/{id}/next?n=10     — the next offers to label, blind, in the representative order
POST /api/quality/calibration/{id}/answers       — save one answer {job_id, answer, reason?} at once
POST /api/quality/calibration/{id}/save          — write the session's answers as a new dataset version
POST /api/quality/calibration/{id}/extend        — "label 100 more"

Each request runs ``python -m src.evaluation.calibration`` in a child process, like the Quality jobs:
production code never imports the evaluation package (ADR 0007). This module only maps its JSON and
refusal codes to HTTP. It never reads or writes ``jobs.db``. Writes go through the same origin and peer
guards as every other mutating route (app middleware).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.quality_jobs import PROJECT_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/quality/calibration", tags=["quality"])

CALIBRATION_DIR = "calibration"       # under the data folder, beside jobs.db
MAX_SESSION = 30
_TIMEOUT_SECONDS = 120
_STATUS = {"not_found": 404, "already_imported": 409, "already_labelled": 409}


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=1024)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=128)
    answer: Literal["relevant", "not_relevant", "not_sure"]
    reason: str | None = Field(default=None, max_length=32)   # one of the tool's reasons, checked there


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.db_path).parent


def _root(request: Request) -> Path:
    return Path(getattr(request.app.state, "calibration_dir", None) or _data_dir(request) / CALIBRATION_DIR)


def _arg(value: str) -> str:
    """A user value passed as a command-line argument: one starting with "-" would read as an option."""
    if value.startswith("-"):
        raise HTTPException(status_code=400, detail={"code": "invalid_argument"})
    return value


def _call(request: Request, *args: str) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "src.evaluation.calibration", str(_root(request)), *args]
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=_TIMEOUT_SECONDS,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = json.loads(proc.stdout or "null")
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.warning("calibration %s failed: %s", args[:1], exc)
        raise HTTPException(status_code=500, detail={"code": "unexpected"}) from exc
    if proc.returncode == 2 and isinstance(out, dict) and "error" in out:
        raise HTTPException(status_code=_STATUS.get(out["error"], 400), detail={"code": out["error"]})
    if proc.returncode != 0 or not isinstance(out, dict):
        logger.warning("calibration %s exited %s: %s", args[:1], proc.returncode, (proc.stderr or "")[-2000:])
        raise HTTPException(status_code=500, detail={"code": "unexpected"})
    return out


@router.get("")
def overview(request: Request) -> dict[str, Any]:
    return _call(request, "list", "--exports", str(_data_dir(request) / "benchmarks"))


@router.post("/import")
def import_export(request: Request, body: ImportIn = Body(...)) -> dict[str, Any]:
    return _call(request, "import", _arg(body.source))


@router.get("/{dataset_id}/next")
def next_items(dataset_id: str, request: Request, n: int = Query(default=10, ge=1, le=MAX_SESSION)) -> dict[str, Any]:
    return _call(request, "next", _arg(dataset_id), str(n))


@router.post("/{dataset_id}/answers")
def answer(dataset_id: str, request: Request, body: AnswerIn = Body(...)) -> dict[str, Any]:
    return _call(request, "answer", _arg(dataset_id), _arg(body.job_id), body.answer,
                 *(["--reason", _arg(body.reason)] if body.reason is not None else []))


@router.post("/{dataset_id}/save")
def save(dataset_id: str, request: Request) -> dict[str, Any]:
    return _call(request, "save", _arg(dataset_id))


@router.post("/{dataset_id}/extend")
def extend(dataset_id: str, request: Request) -> dict[str, Any]:
    return _call(request, "extend", _arg(dataset_id))
