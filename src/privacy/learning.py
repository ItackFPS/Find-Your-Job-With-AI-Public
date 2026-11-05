"""Pause learning and Reset learning (ADR 0015, v5 §4.12 four-actions table).

* **Pause** is a reversible switch in ``app_state``: while paused, the pipeline applies no personal
  adjustment (the legacy personalization stage passes the ranking through unchanged). Signals are
  still recorded.
* **Reset** is a non-destructive marker: ``app_state.learning_reset_at`` is set, learning ignores
  every signal recorded before it, and the derived learned state is cleared
  (``RESET_CLEARED_TABLES``). The journal, the exposure log, hidden offers, the application tracker
  and stored scores are kept.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.storage import app_state
from src.storage.data_classes import RESET_CLEARED_TABLES


@dataclass(frozen=True)
class LearningState:
    paused: bool
    reset_at: str | None


def state(db_path: str) -> LearningState:
    st = app_state.current(db_path)
    return LearningState(st.learning_paused, st.learning_reset_at)


def set_paused(db_path: str, paused: bool) -> LearningState:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            app_state.set_learning_paused(conn, paused)
    finally:
        conn.close()
    return state(db_path)


def is_paused(db_path: str) -> bool:
    """True when the user paused learning; False when the state cannot be read (never blocks a run)."""
    try:
        return app_state.current(db_path).learning_paused
    except sqlite3.Error:
        return False


def reset(db_path: str) -> tuple[str, dict[str, int]]:
    """Set the reset marker and clear the derived tables, in one transaction → (marker, rows cleared).

    Signal times have whole-second precision, so the marker is the **next** whole second and the call
    returns once that second has begun: every signal recorded before the reset is strictly earlier
    than the marker, every signal recorded after it is at or after the marker."""
    now = datetime.now(UTC)
    marker = now.replace(microsecond=0) + timedelta(seconds=1)
    conn = sqlite3.connect(db_path)
    cleared: dict[str, int] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in RESET_CLEARED_TABLES:
                if table in existing:
                    cleared[table] = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608 — fixed names
            at = app_state.mark_learning_reset(conn, marker.isoformat(timespec="seconds"))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    wait = (marker - datetime.now(UTC)).total_seconds()
    if wait > 0:
        time.sleep(wait)
    return at, cleared


def after_reset(occurred_at: str, reset_at: str | None) -> bool:
    """True when a signal recorded at ``occurred_at`` counts for learning (no marker, or after it)."""
    if not reset_at:
        return True
    try:
        return _parse(occurred_at) >= _parse(reset_at)
    except ValueError:
        return occurred_at >= reset_at


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
