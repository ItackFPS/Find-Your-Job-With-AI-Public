"""Settings › Data: how big the database is, and removing old expired offers (issue #86).

Only offers marked expired (src/storage/sqlite.py ``apply_expiry`` / a user report) for
longer than the chosen age are removed, never an offer the user acted on: one with
feedback, an outcome event (applied, viewed…) or an application status is kept, so labels
and the history built from them stay whole. Data derived from a removed offer goes with it
(search index, embedding, provenance, AI analysis, personalization cache). Pipeline-run
history has its own retention (config/retention.yaml). Before anything is removed the
database is copied to ``backups/`` (src/storage/migrations.py ``backup_database``); without
that copy (not enough free disk space) nothing is removed. The file is then compacted
(``VACUUM``), which needs free disk space about the size of the database; a compaction that
fails leaves the removal done and says so (``compacted: False``).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.storage.migrations import backup_database

# An offer referenced here is the user's record and is never removed.
PROTECTING_TABLES: tuple[str, ...] = ("job_feedback", "feedback_events", "outcome_events")
# Rows derived from an offer, removed with it: (table, column holding the offer id).
DERIVED_TABLES: tuple[tuple[str, str], ...] = (
    ("job_fts", "id"),
    ("job_embeddings", "job_id"),
    ("job_provenance", "job_id"),
    ("ai_analysis", "job_id"),
    ("personalization_score_cache", "job_id"),
)
AGE_CHOICES: tuple[int, ...] = (30, 90, 180)


class CleanupRefused(RuntimeError):
    """Nothing was removed: the database could not be copied first."""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def _removable_where(conn: sqlite3.Connection) -> str:
    """SQL condition (one ``?``: the cutoff) for the expired offers that may be removed."""
    tables = _tables(conn)
    keep = [f"id NOT IN (SELECT job_id FROM {t})" for t in PROTECTING_TABLES if t in tables]
    return " AND ".join([
        "status = 'expired'", "expired_at IS NOT NULL", "expired_at < ?", "application_status IS NULL", *keep,
    ])


def database_bytes(db_path: Path) -> int:
    """The database's size on disk, its write-ahead log included."""
    total = 0
    for suffix in ("", "-wal"):
        f = Path(str(db_path) + suffix)
        if f.exists():
            total += f.stat().st_size
    return total


def data_summary(db_path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Size, offers by status, and how many expired offers each age choice would remove."""
    now = now or datetime.now(UTC)
    conn = sqlite3.connect(str(db_path))
    try:
        by_status = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        where = _removable_where(conn)
        removable = {
            str(days): conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}",
                                    ((now - timedelta(days=days)).isoformat(),)).fetchone()[0]
            for days in AGE_CHOICES
        }
    finally:
        conn.close()
    backups_dir = db_path.parent / "backups"
    backups = sorted(backups_dir.glob(f"{db_path.stem}-*.db")) if backups_dir.is_dir() else []
    return {
        "database_bytes": database_bytes(db_path),
        "offers": {"active": by_status.get("active", 0), "expired": by_status.get("expired", 0)},
        "removable_expired": removable,
        "backups": [{"name": b.name, "bytes": b.stat().st_size} for b in backups],
    }


def remove_expired(db_path: Path, older_than_days: int, now: datetime | None = None) -> dict[str, Any]:
    """Copy the database, remove the expired offers older than ``older_than_days`` (and
    their derived rows), then compact the file. Returns ``{"removed", "backup", "compacted",
    "bytes_before", "bytes_after"}``; raises ``CleanupRefused`` when the copy cannot be made."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=older_than_days)).isoformat()
    before = database_bytes(db_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        tables = _tables(conn)
        conn.execute("CREATE TEMP TABLE removed_offers AS SELECT id FROM jobs WHERE " + _removable_where(conn),
                     (cutoff,))
        removed = conn.execute("SELECT COUNT(*) FROM removed_offers").fetchone()[0]
        backup = None
        if removed:
            backup = backup_database(conn, db_path, "removing expired offers", now)
            if backup is None:
                raise CleanupRefused("backup_failed")
        with conn:
            for table, column in DERIVED_TABLES:
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE {column} IN (SELECT id FROM removed_offers)")
            conn.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM removed_offers)")
        conn.execute("DROP TABLE removed_offers")
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            compacted = True
        except sqlite3.OperationalError:  # no disk space for the rewrite, or the file is busy
            compacted = False
    finally:
        conn.close()
    return {"removed": removed, "backup": backup.name if backup else None, "compacted": compacted,
            "bytes_before": before, "bytes_after": database_bytes(db_path)}
