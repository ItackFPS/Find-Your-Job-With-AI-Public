"""Privacy actions API (ADR 0015).

GET  /api/privacy/learning        — {"paused": bool, "reset_at": str | null}
PUT  /api/privacy/learning/pause  — {"paused": true}: pause or resume learning
POST /api/privacy/learning/reset  — {"confirm": true}: set the reset marker, clear derived state
GET  /api/privacy/erase           — the latest erasure job (state, counts, checks; no content)
POST /api/privacy/erase           — {"confirm": true, "keep_tracker": bool, "erase_calibration": bool}
POST /api/privacy/erase/retry     — resume a BLOCKED erasure from its last completed step
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.privacy import erasure, learning

logger = logging.getLogger(__name__)
QUIESCE_TIMEOUT_S = 600.0
_erase_lock = threading.Lock()

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


class LearningOut(BaseModel):
    paused: bool
    reset_at: str | None


class PauseIn(BaseModel):
    paused: bool


class ResetIn(BaseModel):
    confirm: bool = False


class ResetOut(LearningOut):
    cleared: dict[str, int]


@router.get("/learning", response_model=LearningOut)
def get_learning(request: Request) -> LearningOut:
    st = learning.state(request.app.state.db_path)
    return LearningOut(paused=st.paused, reset_at=st.reset_at)


@router.put("/learning/pause", response_model=LearningOut)
def pause_learning(body: PauseIn, request: Request) -> LearningOut:
    st = learning.set_paused(request.app.state.db_path, body.paused)
    return LearningOut(paused=st.paused, reset_at=st.reset_at)


@router.post("/learning/reset", response_model=ResetOut)
def reset_learning(body: ResetIn, request: Request) -> ResetOut:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm_required")
    db = request.app.state.db_path
    _, cleared = learning.reset(db)
    st = learning.state(db)
    return ResetOut(paused=st.paused, reset_at=st.reset_at, cleared=cleared)


# -- Erase my activity (ADR 0015) ----------------------------------------------------------------

class EraseIn(BaseModel):
    confirm: bool = False
    keep_tracker: bool = False
    erase_calibration: bool = True


def _quiesce(app: Any) -> None:
    """Pause the scheduler (asks running scans to pause) and wait until no pipeline run is active."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.pause_all()
    runner = getattr(app.state, "runner", None)
    deadline = time.monotonic() + QUIESCE_TIMEOUT_S
    while runner is not None and runner.is_running():
        if time.monotonic() > deadline:
            raise erasure.Blocked("pipeline_busy: a search run did not stop; retry when it ends")
        time.sleep(0.5)


def _run_erasure(app: Any) -> None:
    if not _erase_lock.acquire(blocking=False):
        return
    try:
        st = erasure.run(app.state.db_path, quiesce=lambda: _quiesce(app))
        if st and st.get("state") == "COMPLETE":
            scheduler = getattr(app.state, "scheduler", None)
            if scheduler is not None:
                scheduler.resume_all()
    except Exception:  # noqa: BLE001 — logged; the job stays pending and can be retried
        logger.exception("Erasure run failed")
    finally:
        _erase_lock.release()


def _start(request: Request) -> None:
    if getattr(request.app.state, "erasure_sync", False):   # tests: run in the request
        _run_erasure(request.app)
        return
    threading.Thread(target=_run_erasure, args=(request.app,), daemon=True, name="erasure").start()


def _status_out(db: str) -> dict[str, Any]:
    st = erasure.status(db) or {"state": None, "pending": erasure.pending(db)}
    st["running"] = _erase_lock.locked()
    return st


@router.get("/erase")
def get_erasure(request: Request) -> dict[str, Any]:
    return _status_out(request.app.state.db_path)


@router.post("/erase", status_code=202)
def erase_activity(body: EraseIn, request: Request) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm_required")
    db = request.app.state.db_path
    if erasure.pending(db):
        raise HTTPException(status_code=409, detail="erasure_pending")
    erasure.request(db, erasure.Scope(keep_tracker=body.keep_tracker, erase_calibration=body.erase_calibration))
    _start(request)
    return _status_out(db)


@router.post("/erase/retry", status_code=202)
def retry_erasure(request: Request) -> dict[str, Any]:
    db = request.app.state.db_path
    if not erasure.pending(db):
        raise HTTPException(status_code=409, detail="no_pending_erasure")
    _start(request)
    return _status_out(db)
