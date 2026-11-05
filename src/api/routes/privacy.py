"""Privacy actions API (ADR 0015).

GET  /api/privacy/learning        — {"paused": bool, "reset_at": str | null}
PUT  /api/privacy/learning/pause  — {"paused": true}: pause or resume learning
POST /api/privacy/learning/reset  — {"confirm": true}: set the reset marker, clear derived state
GET  /api/privacy/erase           — the latest erasure job (state, counts, checks; no content)
POST /api/privacy/erase           — {"confirm": true, "keep_tracker": bool, "erase_calibration": bool}
POST /api/privacy/erase/retry     — resume a BLOCKED erasure from its last completed step
GET  /api/privacy/delete-all      — the pending (or last) "Delete all" state; never opens the database
POST /api/privacy/delete-all      — {"confirm": true}: delete every application file, then restart
POST /api/privacy/delete-all/retry
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.privacy import delete_all, erasure, learning

logger = logging.getLogger(__name__)
QUIESCE_TIMEOUT_S = 600.0
_erase_lock = threading.Lock()
_delete_all_lock = threading.Lock()

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


# -- Delete all application data (ADR 0015) ------------------------------------------------------

class DeleteAllIn(BaseModel):
    confirm: bool = False


def _data_dir(app: Any) -> Path:
    return Path(app.state.db_path).parent


def _quiesce_all(app: Any) -> None:
    """Stop every writer of the data folder: the scheduler for good, the pipeline run, and this
    process's log files under the logs folder (so they can be deleted)."""
    _quiesce(app)
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.stop()
    st = delete_all.read_state(_data_dir(app)) or {}
    logs = Path(st.get("logs_dir") or _data_dir(app).parent / "logs").resolve()
    root = logging.getLogger()
    for handler in list(root.handlers):
        name = getattr(handler, "baseFilename", None)
        if name and Path(name).resolve().is_relative_to(logs):
            root.removeHandler(handler)
            handler.close()


def _run_delete_all(app: Any) -> None:
    if not _delete_all_lock.acquire(blocking=False):
        return
    try:
        st = delete_all.run(_data_dir(app), quiesce=lambda: _quiesce_all(app))
        if st is not None:
            app.state.delete_all_last = st
            if st.get("state") == "COMPLETE":
                app.state.restart_required = True
                app.state.delete_all_blocked = False
    except Exception:  # noqa: BLE001 — logged; the state file keeps it pending for a retry
        logger.exception("Delete all failed")
    finally:
        _delete_all_lock.release()


def _delete_all_out(app: Any) -> dict[str, Any]:
    st = delete_all.read_state(_data_dir(app)) or getattr(app.state, "delete_all_last", None) or {"state": None}
    out = {k: st.get(k) for k in ("state", "last_state", "last_error", "requested_at", "completed_at",
                                  "attempts", "verification", "skipped")}
    out["pending"] = delete_all.pending(_data_dir(app))
    out["running"] = _delete_all_lock.locked()
    out["restart_required"] = bool(getattr(app.state, "restart_required", False))
    return out


def _start_delete_all(request: Request) -> None:
    if getattr(request.app.state, "erasure_sync", False):   # tests: run in the request
        _run_delete_all(request.app)
        return
    threading.Thread(target=_run_delete_all, args=(request.app,), daemon=True, name="delete-all").start()


@router.get("/delete-all")
def get_delete_all(request: Request) -> dict[str, Any]:
    return _delete_all_out(request.app)


@router.post("/delete-all", status_code=202)
def delete_all_data(body: DeleteAllIn, request: Request) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm_required")
    if delete_all.pending(_data_dir(request.app)) or getattr(request.app.state, "restart_required", False):
        raise HTTPException(status_code=409, detail="delete_all_pending")
    try:
        delete_all.request(request.app.state.db_path, request.app.state.config_dir)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="not_a_data_folder") from exc
    _start_delete_all(request)
    return _delete_all_out(request.app)


@router.post("/delete-all/retry", status_code=202)
def retry_delete_all(request: Request) -> dict[str, Any]:
    if not delete_all.pending(_data_dir(request.app)):
        raise HTTPException(status_code=409, detail="no_pending_delete_all")
    _start_delete_all(request)
    return _delete_all_out(request.app)
