"""Source health routes (plan F6.1).

GET  /api/sources/breakers                 — every source's circuit breaker
POST /api/sources/{name}/breaker/reset     — close one breaker now ("retry now")
GET  /api/sources/overview                 — every source for Settings › Sources (issue #82)
PUT  /api/sources/{name}/choice            — turn an unofficial source on or off: {"enabled": true}

The same breaker list is also part of ``GET /api/scheduler/status`` (``source_breakers``).
Mutating routes are covered by the app-wide host, origin and peer guards like every
other POST.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool

from src.pipeline import circuit_breaker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sources", tags=["sources"])


def breaker_rows(db_path: str) -> list[dict[str, Any]]:
    """Every stored breaker, with ``paused`` computed for now."""
    now = datetime.now(UTC)
    rows = []
    for state in sorted(circuit_breaker.load_states(db_path).values(), key=lambda s: s.source):
        row = state.to_row()
        row["paused"] = state.is_paused(now)
        rows.append(row)
    return rows


@router.get("/breakers")
def list_breakers(request: Request) -> list[dict[str, Any]]:
    return breaker_rows(request.app.state.db_path)


@router.post("/{name}/breaker/reset")
def reset_breaker(name: str, request: Request) -> dict[str, Any]:
    """Lift ``name``'s pause and clear its failure count: the next run tries it."""
    try:
        state = circuit_breaker.reset(request.app.state.db_path, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Breaker reset failed for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail="Could not reset the source's breaker") from exc
    if state is None:
        raise HTTPException(status_code=404, detail="No circuit breaker recorded for this source")
    row = state.to_row()
    row["paused"] = False
    return row


# ---------------------------------------------------------------------------
# Settings › Sources (issue #82)
# ---------------------------------------------------------------------------

class SourceChoiceIn(BaseModel):
    enabled: StrictBool


def _access_class(source_type: str | None) -> str | None:
    from src.collectors.registry import CollectorRegistry

    try:
        cls = CollectorRegistry().get_collector_class(source_type or "")
    except KeyError:
        return None
    access: str | None = getattr(cls, "access_class", None)
    return access


def _chosen(request: Request, name: str) -> bool:
    """The choice in effect for an unofficial source, as the scheduler reads it (the stored
    choice, else an observed one: plan rule I1)."""
    from src.app_settings import unofficial_source_enabled

    opted_in = getattr(request.app.state, "source_opted_in", None)
    if callable(opted_in) and name in ("linkedin", "hellowork"):
        return bool(opted_in(name))
    return unofficial_source_enabled(Path(request.app.state.settings_path), name)


def _last_run_by_source(db_path: str) -> tuple[str | None, dict[str, Any]]:
    """The start and per-source results of the latest pipeline run that has them (a run in
    progress has none yet)."""
    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=db_path)
    try:
        rs.init_db()
        runs = rs.list_runs(limit=5)
    finally:
        rs.close()
    for run in runs:
        by_source = (run.get("stats") or {}).get("by_source")
        if isinstance(by_source, dict) and by_source:
            return run.get("started_at"), by_source
    return None, {}


@router.get("/overview")
def sources_overview(request: Request) -> dict[str, Any]:
    """Every source the app can collect from, for Settings › Sources: its kind (official,
    public page, unofficial), whether it is collected, the user's choice for an unofficial
    one, its result in the latest search, and its circuit breaker."""
    from src.app_settings import UNOFFICIAL_CHOICE_SOURCES, unofficial_source_chosen_at
    from src.discovery.registry import SourceRegistry

    state = request.app.state
    settings_path = Path(state.settings_path)
    entries: dict[str, dict[str, Any]] = {}
    for src in SourceRegistry(config_path=str(Path(state.config_dir) / "sources.yaml")).all_sources:
        entries[src["name"]] = {"type": src.get("type"), "configured": bool(src.get("active", True))}
    # LinkedIn and HelloWork are collected by their own scanners (src/api/staged_sources.py).
    linkedin_config = getattr(state, "linkedin_config", None)
    entries["linkedin"] = {"type": "linkedin", "configured": bool(getattr(linkedin_config, "enabled", False))}
    if "hellowork" in entries:
        entries["hellowork"]["configured"] = getattr(state, "hellowork_scanner", None) is not None

    run_started_at, by_source = _last_run_by_source(state.db_path)
    breakers = {row["source"]: row for row in breaker_rows(state.db_path)}
    rows = []
    for name, entry in sorted(entries.items()):
        unofficial = name in UNOFFICIAL_CHOICE_SOURCES
        if not entry["configured"] and not unofficial:
            continue  # switched off in config/sources.yaml: not offered in the app
        stats = by_source.get(name) if isinstance(by_source.get(name), dict) else None
        access = "unofficial" if unofficial else (
            (stats or {}).get("access_class") or _access_class(entry["type"]))
        choice = None
        if unofficial:
            choice = {"enabled": _chosen(request, name),
                      "chosen_at": unofficial_source_chosen_at(settings_path, name)}
        rows.append({
            "name": name,
            "access_class": access,
            "available": entry["configured"],
            "collected": entry["configured"] and (choice is None or choice["enabled"]),
            "choice": choice,
            "last_run": None if stats is None else {
                "started_at": run_started_at,
                **{k: stats.get(k) for k in ("status", "reason", "collected", "accepted", "errors")},
            },
            "breaker": breakers.get(name),
        })
    return {"sources": rows}


@router.put("/{name}/choice")
def set_source_choice(name: str, body: SourceChoiceIn, request: Request) -> dict[str, Any]:
    """Record the user's choice for an unofficial source, with its date (F6.5)."""
    from src.app_settings import set_unofficial_source

    try:
        return set_unofficial_source(Path(request.app.state.settings_path), name, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
