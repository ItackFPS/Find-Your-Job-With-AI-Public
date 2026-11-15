"""What was recorded: a read-only summary of the behaviour data kept on this computer (Phase B B9-A).

The user can see, without changing anything, every kind of event the application stored about their
actions, how many of each, where they came from (the list's surface), which ones learning may still
count (after the last reset, not superseded), and the visible exposures per list and per source with
their retention. Nothing here writes; erasing stays the Erase action (ADR 0015).

Repeated views (Phase B B9-A decision): every opening of an offer is recorded, but learning counts
an offer's views once (``src/learning/signals.py``), so a repeated view carries no extra signal.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from src.feedback.exposure import COMPACT_AFTER, KEEP_FOR
from src.storage import app_state

NO_SURFACE = "unknown"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def summary(db_path: str | Path) -> dict[str, Any]:
    """The summary the Settings page shows. Missing tables read as nothing recorded."""
    path = Path(db_path)
    state = app_state.current(str(path)) if path.exists() else None
    reset_at = state.learning_reset_at if state else None
    out: dict[str, Any] = {
        "learning": {"paused": bool(state and state.learning_paused), "reset_at": reset_at},
        "events": [], "event_surfaces": {}, "exposures": {
            "total": 0, "by_surface": {}, "by_source": {}, "oldest_at": None,
            "compact_after_days": COMPACT_AFTER.days, "keep_days": KEEP_FOR.days,
        },
        "repeated_views": "counted once per offer",
    }
    if not path.exists():
        return out
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if _table_exists(conn, "feedback_events"):
            has_superseded = "superseded" in _columns(conn, "feedback_events")
            rows = conn.execute(
                "SELECT type, signal_class, occurred_at, metadata_json"
                + (", superseded" if has_superseded else ", 0 AS superseded")
                + " FROM feedback_events").fetchall()
            per_type: dict[tuple[str, str], dict[str, Any]] = {}
            surfaces: Counter[str] = Counter()
            for r in rows:
                key = (r["type"], r["signal_class"])
                row = per_type.setdefault(key, {"type": r["type"], "signal_class": r["signal_class"],
                                                "count": 0, "counted": 0, "last_at": None})
                row["count"] += 1
                if not r["superseded"] and (reset_at is None or str(r["occurred_at"]) >= reset_at):
                    row["counted"] += 1
                if row["last_at"] is None or str(r["occurred_at"]) > row["last_at"]:
                    row["last_at"] = str(r["occurred_at"])
                try:
                    ctx = (json.loads(r["metadata_json"] or "{}") or {}).get("context") or {}
                except (ValueError, AttributeError):
                    ctx = {}
                surfaces[str(ctx.get("surface") or NO_SURFACE)] += 1
            out["events"] = sorted(per_type.values(), key=lambda e: (e["signal_class"], e["type"]))
            out["event_surfaces"] = dict(sorted(surfaces.items()))
        if _table_exists(conn, "exposure_log"):
            exp = out["exposures"]
            exp["total"] = conn.execute("SELECT COUNT(*) FROM exposure_log").fetchone()[0]
            exp["by_surface"] = {r[0]: r[1] for r in conn.execute(
                "SELECT surface, COUNT(*) FROM exposure_log GROUP BY surface ORDER BY surface")}
            exp["oldest_at"] = conn.execute("SELECT MIN(occurred_at) FROM exposure_log").fetchone()[0]
            if _table_exists(conn, "jobs"):
                # Source and volume bias (09 §5.3): sources that return many offers dominate exposures.
                exp["by_source"] = {str(r[0] or NO_SURFACE): r[1] for r in conn.execute(
                    "SELECT j.source, COUNT(*) FROM exposure_log e LEFT JOIN jobs j ON j.id = e.job_id "
                    "GROUP BY j.source ORDER BY j.source")}
    finally:
        conn.close()
    return out
