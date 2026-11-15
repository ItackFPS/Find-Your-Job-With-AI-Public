"""Collection freeze routes (owner decision D7, ADR 0032, ``src/collectors/freeze.py``).

GET  /api/collection/status                 — whether collection is frozen, why, and each collector's state
POST /api/collection/{source_type}/reactivate — turn one VALIDATED collector back on (the user's action)
POST /api/collection/{source_type}/freeze     — freeze one collector again

Mutating routes are covered by the app-wide host, origin and peer guards like every other POST.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from src.collectors import freeze

router = APIRouter(prefix="/api/collection", tags=["collection"])


def _settings(request: Request) -> Path | None:
    """The app's settings file; None when the app has none (then everything reads as frozen)."""
    path = getattr(request.app.state, "settings_path", None)
    return Path(path) if path else None


def _config_dir(request: Request) -> str:
    return str(getattr(request.app.state, "config_dir", "config"))


def refuse_frozen(request: Request, source_type: str) -> None:
    """409 when ``source_type``'s collector is frozen: used by every route that starts a scan."""
    if not freeze.collector_allowed(_settings(request), source_type):
        raise HTTPException(status_code=409, detail=freeze.FROZEN_REASON)


@router.get("/status")
def collection_status(request: Request) -> dict[str, Any]:
    return freeze.status(_settings(request), _config_dir(request))


@router.post("/{source_type}/reactivate")
def reactivate_collector(source_type: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    if settings is None:
        raise HTTPException(status_code=503, detail="The application has no settings file.")
    try:
        return freeze.reactivate(settings, source_type)
    except freeze.CollectorNotValidated as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{source_type}/freeze")
def freeze_collector(source_type: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    if settings is None:
        raise HTTPException(status_code=503, detail="The application has no settings file.")
    freeze.deactivate(settings, source_type)
    return {"type": source_type, "allowed": freeze.collector_allowed(settings, source_type)}
