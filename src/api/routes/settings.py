"""Application settings API (update plan F2.4).

GET /api/settings/notifications  — the desktop notification categories, their state and availability
PUT /api/settings/notifications  — turn categories on or off: {"categories": {"source_paused": true}}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.app_settings import NOTIFICATION_AVAILABLE, notification_settings, update_notification_settings

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
