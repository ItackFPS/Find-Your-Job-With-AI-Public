"""Application settings API (update plan F2.4).

GET /api/settings/notifications  — the desktop notification categories, their state and availability
PUT /api/settings/notifications  — turn categories on or off: {"categories": {"source_paused": true}}
GET /api/settings/background     — background collection: {"enabled": true, "daily_time": "08:00"}
PUT /api/settings/background     — change either value (issue #80)
GET /api/settings/data           — database size, offers by status, cleanup choices and state (issue #86)
POST /api/settings/data/cleanup  — remove expired offers older than N days, then compact: {"older_than_days": 90}
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from src.app_settings import (
    NOTIFICATION_AVAILABLE,
    background_settings,
    notification_settings,
    update_background_settings,
    update_notification_settings,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class NotificationSettingsIn(BaseModel):
    categories: dict[str, Any]


def _path(request: Request) -> Path:
    return Path(request.app.state.settings_path)


def _body(categories: dict[str, bool]) -> dict[str, Any]:
    return {"categories": categories, "available": dict(NOTIFICATION_AVAILABLE)}


@router.get("/notifications")
def get_notification_settings(request: Request) -> dict[str, Any]:
    return _body(notification_settings(_path(request)))


@router.put("/notifications")
def put_notification_settings(body: NotificationSettingsIn, request: Request) -> dict[str, Any]:
    try:
        return _body(update_notification_settings(_path(request), body.categories))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class BackgroundSettingsIn(BaseModel):
    enabled: Any = None
    daily_time: Any = None


@router.get("/background")
def get_background_settings(request: Request) -> dict[str, Any]:
    return background_settings(_path(request))


@router.put("/background")
def put_background_settings(body: BackgroundSettingsIn, request: Request) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    try:
        return update_background_settings(_path(request), changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Settings › Data (issue #86)
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


class CleanupIn(BaseModel):
    older_than_days: StrictInt = Field(ge=7, le=3650)


class _Cleanup:
    """The one cleanup that may run at a time, in the background, and how it ended."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"state": "idle"}


_cleanup = _Cleanup()


def _busy(request: Request) -> str | None:
    """Why a cleanup cannot start now (it needs the database to itself), or None."""
    state = request.app.state
    runner = getattr(state, "runner", None)
    if runner is not None and runner.is_running():
        return "search_running"
    for name in ("linkedin_scanner", "hellowork_scanner"):
        scanner = getattr(state, name, None)
        if scanner is not None and scanner.is_scanning():
            return "scan_running"
    return None


@router.get("/data")
def get_data_settings(request: Request) -> dict[str, Any]:
    from src.storage.cleanup import data_summary

    return {**data_summary(Path(request.app.state.db_path)), "cleanup": dict(_cleanup.state)}


@router.post("/data/cleanup", status_code=202)
def start_data_cleanup(body: CleanupIn, request: Request) -> dict[str, Any]:
    """Remove expired offers older than ``older_than_days`` (never one the user acted on),
    then compact the database, in the background. 409 while a search or scan runs."""
    from src.storage.cleanup import remove_expired

    reason = _busy(request)
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)
    if not _cleanup.lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="cleanup_running")
    db_path = Path(request.app.state.db_path)
    scheduler = getattr(request.app.state, "scheduler", None)
    # No scheduled search or scan may start while the file is rewritten.
    paused_here = scheduler is not None and not scheduler.paused
    if paused_here:
        scheduler.pause_all()
    _cleanup.state = {"state": "running", "older_than_days": body.older_than_days,
                      "started_at": datetime.now(UTC).isoformat()}

    def run() -> None:
        try:
            result = remove_expired(db_path, body.older_than_days)
            _cleanup.state = {**_cleanup.state, "state": "done", **result,
                              "finished_at": datetime.now(UTC).isoformat()}
        except Exception as exc:  # noqa: BLE001 — reported to the page, never raised
            logger.warning("Data cleanup failed: %s", exc)
            _cleanup.state = {**_cleanup.state, "state": "failed", "error": str(exc),
                              "finished_at": datetime.now(UTC).isoformat()}
        finally:
            if paused_here:
                scheduler.resume_all()
            _cleanup.lock.release()

    threading.Thread(target=run, daemon=True, name="data-cleanup").start()
    return dict(_cleanup.state)
