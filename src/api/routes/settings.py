"""Application settings API (update plan F2.4).

GET /api/settings/notifications  — the desktop notification categories, their state and availability
PUT /api/settings/notifications  — turn categories on or off: {"categories": {"source_paused": true}}
GET /api/settings/background     — background collection: {"enabled": true, "daily_time": "08:00"}
PUT /api/settings/background     — change either value (issue #80)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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
