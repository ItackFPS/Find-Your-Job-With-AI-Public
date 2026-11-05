"""Visible exposure (ADR 0016): which offers the user actually saw, as context for learning, never as
evidence on its own.

An offer counts as *visible* when at least half its card stayed in the viewport for at least one
second while the tab was visible; the frontend batches such observations and posts them best-effort.
A rendered card is never logged. Batches carry the erasure token (``<data_epoch>:<generation>``); a
batch with a missing or stale token is dropped silently (ADR 0015).

Retention (ADR 0015, initial values): rows older than 30 days are compacted to one row per
(offer, surface) with the first visible time and best position, without ``visible_ms``; rows older
than 180 days are deleted. The table is **personal** (``src/storage/data_classes.py``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.storage import app_state

COMPACT_AFTER = timedelta(days=30)
KEEP_FOR = timedelta(days=180)
MAX_BATCH = 200


@dataclass(frozen=True)
class ExposureItem:
    job_id: str
    surface: str
    position: int | None = None
    visible_ms: int | None = None
    sort_key: str | None = None
    randomized: bool = False
    client_created_at: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class ExposureStorage:
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db") -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_db(self) -> None:
        key = str(self._db_path.resolve())
        if key in self._initialized_paths and self._db_path.exists():
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS exposure_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                surface TEXT NOT NULL,
                position INTEGER,
                visible_ms INTEGER,
                sort_key TEXT,
                randomized INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                generation INTEGER NOT NULL,
                client_created_at TEXT,
                occurred_at TEXT NOT NULL,
                compacted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_exposure_job ON exposure_log(job_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_exposure_time ON exposure_log(occurred_at);
        """)
        app_state.ensure(conn)
        conn.commit()
        self._initialized_paths.add(key)

    def record_batch(self, token: str | None, session_id: str | None, items: list[ExposureItem],
                     now: datetime | None = None) -> int:
        """Store a batch → rows stored. A missing or stale token drops the whole batch (0)."""
        if not token or not items:
            return 0
        at = (now or _now()).isoformat(timespec="seconds")
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                state = app_state.check_token(conn, token)
            except app_state.GenerationMismatch:
                conn.rollback()
                return 0
            conn.executemany(
                """INSERT INTO exposure_log (job_id, surface, position, visible_ms, sort_key, randomized,
                       session_id, generation, client_created_at, occurred_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(i.job_id, i.surface, i.position, i.visible_ms, i.sort_key, int(i.randomized), session_id,
                  state.erasure_generation, i.client_created_at, at) for i in items[:MAX_BATCH]])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return min(len(items), MAX_BATCH)

    def apply_retention(self, now: datetime | None = None) -> dict[str, int]:
        """Compact rows older than ``COMPACT_AFTER`` and delete rows older than ``KEEP_FOR``."""
        now = now or _now()
        delete_before = (now - KEEP_FOR).isoformat(timespec="seconds")
        compact_before = (now - COMPACT_AFTER).isoformat(timespec="seconds")
        conn = self._get_conn()
        with conn:
            deleted = conn.execute("DELETE FROM exposure_log WHERE occurred_at < ?", (delete_before,)).rowcount
            groups = conn.execute(
                """SELECT job_id, surface, MIN(occurred_at) AS first_at, MIN(position) AS best_pos,
                          MAX(generation) AS gen, COUNT(*) AS n
                   FROM exposure_log WHERE occurred_at < ? AND compacted = 0
                   GROUP BY job_id, surface""", (compact_before,)).fetchall()
            compacted = 0
            for g in groups:
                conn.execute("DELETE FROM exposure_log WHERE job_id = ? AND surface = ? AND occurred_at < ? "
                             "AND compacted = 0", (g["job_id"], g["surface"], compact_before))
                conn.execute(
                    """INSERT INTO exposure_log (job_id, surface, position, generation, occurred_at, compacted)
                       VALUES (?,?,?,?,?,1)""", (g["job_id"], g["surface"], g["best_pos"], g["gen"], g["first_at"]))
                compacted += int(g["n"])
        return {"deleted": int(deleted), "compacted": compacted}

    def count(self) -> int:
        return int(self._get_conn().execute("SELECT COUNT(*) FROM exposure_log").fetchone()[0])

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._get_conn().execute("SELECT * FROM exposure_log ORDER BY id")]
