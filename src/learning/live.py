"""The personal ranking the user sees, and how they inspect and correct it (ADR 0030).

ADR 0019's activation rule stays computed, but it cannot decide at the signal volumes one person
produces (docs/design/learning-architecture-decisions.md, L3 simulator). The order the user sees is
therefore under their control instead of under a statistical gate:

* it applies once there is enough **strong** evidence (a saved, applied, interested or rejected
  offer, never a mere opening), and unless they paused learning;
* its effect stays bounded: the adjustment is within the evidence-scaled ``cap(n)``, and the scoring
  gates are re-applied, so a confirmed mismatch cannot rise;
* stored scores never change: the production order and the personal order are both journaled in
  ``ranking_shadow``, which is what makes the before/after comparison possible;
* every learned preference can be read and forgotten here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.learning.config import PersonalModelConfig
from src.learning.model import EMBEDDING_LABEL
from src.learning.signals import ALL_FEATURES

Ranked = list[tuple[Any, float]]
GateCap = Callable[[Ranked, Ranked], Ranked]

_OVERRIDES_DDL = """
CREATE TABLE IF NOT EXISTS personal_preference_overrides (
    feature TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (feature, value)
);
"""
MAX_LISTED = 40
MAX_MOVERS = 8


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_OVERRIDES_DDL)


def live_order(production: Sequence[tuple[Any, float]], adjustments: dict[str, float], strong: int,
               cfg: PersonalModelConfig, gate_cap: GateCap) -> Ranked | None:
    """The personal order → None keeps the production order (off, or not enough strong evidence).

    Scores in the result are ordering keys only. Raises ``ValueError`` when the result is not a
    permutation of production (the caller then keeps production)."""
    if not cfg.live_ranking or strong < cfg.live_min_strong:
        return None
    ranked = sorted(((job, score + adjustments.get(job.id, 0.0)) for job, score in production),
                    key=lambda p: p[1], reverse=True)
    ranked = gate_cap(list(production), ranked)
    if sorted(job.id for job, _ in ranked) != sorted(job.id for job, _ in production):
        raise ValueError("personal ranking is not a permutation of production")
    return ranked


def forgotten(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    try:
        return {(f, v) for f, v in conn.execute("SELECT feature, value FROM personal_preference_overrides")}
    except sqlite3.OperationalError:
        return set()


def _check(feature: str) -> None:
    if feature not in ALL_FEATURES:
        raise ValueError(f"unknown feature {feature!r}")


def forget(db_path: str | Path, feature: str, value: str, now: datetime | None = None) -> None:
    """The learned preference no longer counts, from the next search on. The journal is untouched."""
    _check(feature)
    moment = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        with conn:
            conn.execute("INSERT OR REPLACE INTO personal_preference_overrides (feature, value, created_at) "
                         "VALUES (?, ?, ?)", (feature, value, moment))
    finally:
        conn.close()


def restore(db_path: str | Path, feature: str, value: str) -> None:
    _check(feature)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        with conn:
            conn.execute("DELETE FROM personal_preference_overrides WHERE feature = ? AND value = ?", (feature, value))
    finally:
        conn.close()


def _state(conn: sqlite3.Connection) -> tuple[str | None, dict[str, Any]]:
    try:
        row = conn.execute("SELECT run_id, summary_json FROM personal_model_state WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None, {}
    return (row[0], json.loads(row[1] or "{}")) if row else (None, {})


def comparison(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """How the personal order differs from the production order in one run."""
    rows = conn.execute(
        "SELECT rs.job_id, rs.production_position, rs.challenger_position, rs.personal_adjustment, "
        "rs.explanation_json, j.title, j.company FROM ranking_shadow rs LEFT JOIN jobs j ON j.id = rs.job_id "
        "WHERE rs.run_id = ?", (run_id,)).fetchall()
    applied = conn.execute("SELECT COUNT(*) FROM job_scores WHERE run_id = ? AND visible_position IS NOT NULL",
                           (run_id,)).fetchone()[0] > 0
    moved = [r for r in rows if r[1] != r[2]]
    movers = sorted(moved, key=lambda r: abs(r[1] - r[2]), reverse=True)[:MAX_MOVERS]
    top = {r[0] for r in rows if r[1] <= 20}
    personal_top = {r[0] for r in rows if r[2] <= 20}
    return {
        "run_id": run_id, "applied": applied, "ranked": len(rows), "moved": len(moved),
        "entered_top_20": len(personal_top - top),
        "movers": [{"job_id": r[0], "title": r[5], "company": r[6], "production_position": r[1],
                    "personal_position": r[2], "adjustment": round(r[3], 4),
                    "reasons": json.loads(r[4] or "[]")} for r in movers],
    }


def preferences(db_path: str | Path, cfg: PersonalModelConfig) -> dict[str, Any]:
    """What the app has learned, whether it applies, and what it changed in the latest search."""
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        run_id, summary = _state(conn)
        hidden = sorted(forgotten(conn))
        items = [{"feature": feature, "value": value, "weight": round(weight, 4)}
                 for feature, table in (summary.get("tables") or {}).items() for value, weight in table.items()]
        items.sort(key=lambda i: abs(i["weight"]), reverse=True)
        try:
            compared = comparison(conn, run_id) if run_id else None
        except sqlite3.OperationalError:
            compared = None
    finally:
        conn.close()
    strong = int(summary.get("strong") or 0)
    return {
        "signals": int(summary.get("n") or 0), "strong_signals": strong, "strong_needed": int(cfg.live_min_strong),
        "enough_evidence": strong >= cfg.live_min_strong, "cap": float(summary.get("cap") or 0.0),
        "preferences": items[:MAX_LISTED],
        "forgotten": [{"feature": f, "value": v} for f, v in hidden],
        "comparison": compared,
    }


def why(db_path: str | Path, job_id: str) -> dict[str, Any] | None:
    """Why the latest personal order moved this offer → None when it has no personal position."""
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            row = conn.execute(
                "SELECT rs.production_position, rs.challenger_position, rs.personal_adjustment, rs.explanation_json, "
                "js.visible_position FROM ranking_shadow rs "
                "JOIN personal_model_state st ON st.run_id = rs.run_id "
                "LEFT JOIN job_scores js ON js.run_id = rs.run_id AND js.job_id = rs.job_id "
                "WHERE rs.job_id = ?", (job_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()
    if row is None:
        return None
    return {"production_position": row[0], "personal_position": row[1], "adjustment": round(row[2], 4),
            "reasons": json.loads(row[3] or "[]"), "applied": row[4] is not None}


__all__ = ["EMBEDDING_LABEL", "comparison", "ensure_schema", "forget", "forgotten", "live_order", "preferences",
           "restore", "why"]
