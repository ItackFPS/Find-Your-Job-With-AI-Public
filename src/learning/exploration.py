"""Découverte (S4): a randomized draw independent of both rankings (ADR 0019; stage L4.3).

The eligible pool E of a run holds the offers that reached scoring (they pass the validity rules and
every STRONG constraint: hard filter and requirements gate), are not hidden, and were not shown in
Découverte during the last ``window_days``. Nothing else enters: no score, rank or floor, production
or challenger. The draw is uniform over E with a recorded seed; each drawn offer's inclusion
probability is ``k / |E|``. It happens before ranking, from offer ids only, so it never depends on
either ranking. Drawn offers leave the main list (S1) of that run but keep their positions in
``ranking_shadow``, so discordance is measured as for any other offer.
"""

from __future__ import annotations

import random
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

SLOTS = 2
WINDOW_DAYS = 14

DDL = """
CREATE TABLE IF NOT EXISTS s4_draw (
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    pi REAL NOT NULL,
    seed TEXT NOT NULL,
    pool_size INTEGER NOT NULL,
    drawn_at TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_s4_draw_run ON s4_draw(run_id);
"""


@dataclass(frozen=True)
class Draw:
    run_id: str
    seed: str
    pool_size: int
    drawn: tuple[str, ...] = field(default_factory=tuple)   # slot order (already random)

    @property
    def pi(self) -> float:
        return len(self.drawn) / self.pool_size if self.pool_size else 0.0


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def load_slots(path: str | Path) -> int:
    """``personal_model.exploration.slots`` (default 2, at most 2; 0 turns Découverte off)."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return SLOTS
    section = ((data.get("personal_model") or {}).get("exploration") or {})
    value = section.get("slots", SLOTS)
    return max(0, min(SLOTS, value)) if isinstance(value, int) and not isinstance(value, bool) else SLOTS


def _excluded(conn: sqlite3.Connection, now: datetime, window_days: int) -> set[str]:
    """Hidden offers and offers shown in Découverte during the window."""
    out: set[str] = set()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "feedback_events" in tables:
        state: dict[str, bool] = {}
        for job_id, kind in conn.execute(
                "SELECT job_id, type FROM feedback_events WHERE signal_class = 'list' AND superseded = 0 "
                "AND type IN ('hidden', 'unhidden') ORDER BY rowid"):
            state[job_id] = kind == "hidden"
        out |= {j for j, hidden in state.items() if hidden}
    if "exposure_log" in tables:
        since = (now - timedelta(days=window_days)).isoformat()
        out |= {r[0] for r in conn.execute(
            "SELECT DISTINCT job_id FROM exposure_log WHERE surface = 'decouverte' AND occurred_at >= ?", (since,))}
    return out


def draw(db_path: str | Path, run_id: str, candidate_ids: Iterable[str], *, slots: int = SLOTS,
         window_days: int = WINDOW_DAYS, now: datetime | None = None, seed: str | None = None) -> Draw:
    """Draw up to ``slots`` offers uniformly from the eligible pool and record them in ``s4_draw``."""
    now = now or datetime.now(UTC)
    seed = seed or secrets.token_hex(8)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        ensure_schema(conn)
        pool = sorted(set(candidate_ids) - _excluded(conn, now, window_days))   # sorted: the seed alone decides
        k = min(max(slots, 0), len(pool))
        drawn = tuple(random.Random(seed).sample(pool, k)) if k else ()
        result = Draw(run_id, seed, len(pool), drawn)
        with conn:
            conn.execute("DELETE FROM s4_draw WHERE run_id = ?", (run_id,))
            conn.executemany(
                "INSERT INTO s4_draw (run_id, job_id, slot, pi, seed, pool_size, drawn_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, job_id, slot, result.pi, seed, len(pool), now.isoformat(timespec="seconds"))
                 for slot, job_id in enumerate(drawn, start=1)])
        return result
    finally:
        conn.close()


def record_in_shadow(db_path: str | Path, result: Draw) -> None:
    """Fill ``ranking_shadow.s4_*`` for the run: seed and |E| on every row, π on the drawn offers."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        with conn:
            conn.execute("UPDATE ranking_shadow SET s4_seed = ?, s4_pool_size = ? WHERE run_id = ?",
                         (result.seed, result.pool_size, result.run_id))
            conn.executemany("UPDATE ranking_shadow SET s4_pi = ? WHERE run_id = ? AND job_id = ?",
                             [(result.pi, result.run_id, job_id) for job_id in result.drawn])
    finally:
        conn.close()


def drawn_for_run(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    """The Découverte slots of a run, in slot order → [] when none (or the table does not exist)."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT job_id, slot, pi, seed, pool_size FROM s4_draw WHERE run_id = ? ORDER BY slot",
                            (run_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]
