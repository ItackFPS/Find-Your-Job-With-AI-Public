"""Source health routes (plan F6.1).

GET  /api/sources/breakers                 — every source's circuit breaker
POST /api/sources/{name}/breaker/reset     — close one breaker now ("retry now")

The same breaker list is also part of ``GET /api/scheduler/status`` (``source_breakers``).
Mutating routes are covered by the app-wide host, origin and peer guards like every
other POST.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

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
