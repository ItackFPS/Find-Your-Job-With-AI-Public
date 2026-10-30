"""Database migrations in one place (completion plan P1.3, issue #84).

Two kinds, both idempotent and run from each storage class's ``init_db``:

* **Additive columns** (``ensure_columns``): a column added to a table after
  databases already existed. SQLite adds a nullable or defaulted column without
  rebuilding the table, so this runs at every start and does nothing once the
  column is there.
* **One-shot data migrations** (``DataMigration``, ``apply_data_migrations``):
  statements that must run exactly once per database (a backfill, a purge).
  Each is recorded by name in the ``db_migrations`` table; the list is ordered
  and a name is never reused or renamed, or existing databases would run it
  again.

``JOBS_COLUMNS`` and ``JOBS_DATA_MIGRATIONS`` describe ``jobs.db``'s own
evolution (src/storage/sqlite.py). Append new entries at the end.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# A database at least this large is copied before a data migration runs on it (issue #86).
BACKUP_MIN_BYTES = 200 * 1024 * 1024
# Backups kept in <database folder>/backups.
BACKUPS_KEPT = 2


def ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> list[str]:
    """Add each column of ``columns`` (name -> SQL declaration) that ``table`` lacks, in
    order. Returns the columns added."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    added = []
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            added.append(name)
    return added


@dataclass(frozen=True)
class DataMigration:
    """A one-shot change to existing rows, run once per database and recorded by ``name``."""

    name: str
    statements: tuple[str, ...]


_DDL_MIGRATIONS_TABLE = """
            CREATE TABLE IF NOT EXISTS db_migrations (
                name    TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """


def pending_data_migrations(conn: sqlite3.Connection, migrations: Iterable[DataMigration]) -> list[str]:
    """Names of the migrations not yet recorded in ``db_migrations``."""
    conn.execute(_DDL_MIGRATIONS_TABLE)
    done = {row[0] for row in conn.execute("SELECT name FROM db_migrations")}
    return [m.name for m in migrations if m.name not in done]


def backup_before_migrating(conn: sqlite3.Connection, db_path: Path, now: datetime | None = None) -> bool:
    """Copy a large database before its data migrations run (issue #86).

    Below ``BACKUP_MIN_BYTES`` nothing is copied. Otherwise the database is copied with
    ``backup_database``. Returns False, after logging why, only when the copy cannot be made
    (not enough free disk space, or an error): the caller then leaves the migrations for
    the next start rather than running them without a backup."""
    try:
        size = db_path.stat().st_size
    except OSError:
        return True  # a new or in-memory database: nothing to keep
    if size < BACKUP_MIN_BYTES:
        return True
    return backup_database(conn, db_path, "migrating", now) is not None


def backup_database(conn: sqlite3.Connection, db_path: Path, purpose: str,
                    now: datetime | None = None) -> Path | None:
    """Copy the database to ``<folder>/backups/<name>-<UTC time>.db`` with SQLite's backup
    API, keeping the last ``BACKUPS_KEPT``, before ``purpose`` (a data migration, a
    cleanup). Returns the copy, or None after logging why it could not be made (not
    enough free disk space, or an error)."""
    try:
        size = db_path.stat().st_size
    except OSError as exc:
        logger.warning("Database backup before %s: cannot read %s (%s).", purpose, db_path, exc)
        return None
    backups = db_path.parent / "backups"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(backups).free
    except OSError as exc:
        logger.warning("Database backup before %s: cannot prepare %s (%s).", purpose, backups, exc)
        return None
    if free < size * 1.1:
        logger.warning("Database backup before %s: %d MB needed, %d MB free on %s.",
                       purpose, size // 2**20, free // 2**20, backups)
        return None
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = backups / f"{db_path.stem}-{stamp}.db"
    started = time.perf_counter()
    try:
        conn.commit()
        dest = sqlite3.connect(str(target))
        try:
            conn.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        logger.warning("Database backup before %s failed (%s).", purpose, exc)
        target.unlink(missing_ok=True)
        return None
    logger.info("Database backed up to %s before %s (%d MB in %.1f s).",
                target, purpose, size // 2**20, time.perf_counter() - started)
    for old in sorted(backups.glob(f"{db_path.stem}-*.db"))[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    return target


def apply_data_migrations(conn: sqlite3.Connection, migrations: Iterable[DataMigration]) -> list[str]:
    """Run, in order, each migration not yet recorded in ``db_migrations`` (created if
    missing), and record it. Returns the names applied. The caller commits."""
    conn.execute(_DDL_MIGRATIONS_TABLE)
    applied = []
    for migration in migrations:
        done = conn.execute("SELECT 1 FROM db_migrations WHERE name = ?", (migration.name,)).fetchone()
        if done:
            continue
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute("INSERT INTO db_migrations (name) VALUES (?)", (migration.name,))
        applied.append(migration.name)
    return applied


# ---------------------------------------------------------------------------
# jobs.db
# ---------------------------------------------------------------------------

# Columns added to ``jobs`` after its first release (all nullable).
JOBS_COLUMNS: dict[str, str] = {
    "department": "TEXT",
    "work_mode": "TEXT",
    "canonical_url": "TEXT",
    "apply_url": "TEXT",
    "application_status": "TEXT",
    # Expiry (issue #86).
    "valid_through": "TEXT",
    "missed_runs": "INTEGER NOT NULL DEFAULT 0",
    "expired_at": "TEXT",
    "expiry_reason": "TEXT",
}

# Where each stored offer was seen (issue #86): one row per (offer, source), so an offer
# several sources list is shown once with all of them.
JOB_PROVENANCE_DDL = """
            CREATE TABLE IF NOT EXISTS job_provenance (
                job_id        TEXT NOT NULL,
                source        TEXT NOT NULL,
                source_job_id TEXT,
                url           TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL,
                PRIMARY KEY (job_id, source)
            );
            CREATE INDEX IF NOT EXISTS idx_job_provenance_source ON job_provenance(source);
        """

JOBS_DATA_MIGRATIONS: tuple[DataMigration, ...] = (
    # Purge stale rows that have no canonical_url from sources that always provide one.
    # These rows were stored before the canonical_url column existed. EURES is excluded —
    # its API summary endpoint never returns job URLs. The next pipeline run re-inserts
    # the purged rows with canonical_url set via INSERT OR REPLACE on their stable IDs.
    DataMigration(
        "purge_jobs_without_canonical_url",
        ("DELETE FROM jobs WHERE canonical_url IS NULL AND source != 'eures'",),
    ),
    # Normalize LinkedIn country codes stored as ISO "FR" to the canonical full name
    # "France". Rows were written by an older pipeline run that did not call
    # normalize_country_code before storage. The jobs table stores countries as a JSON
    # array; the exact string '["FR"]' is targeted to avoid touching other sources or
    # valid country lists.
    DataMigration(
        "normalize_linkedin_fr_to_france",
        ("UPDATE jobs SET countries = '[\"France\"]' WHERE source = 'linkedin' AND countries = '[\"FR\"]'",),
    ),
    # Each stored offer's own source becomes its first provenance (issue #86).
    DataMigration(
        "backfill_job_provenance",
        ("INSERT OR IGNORE INTO job_provenance (job_id, source, source_job_id, url, first_seen_at, last_seen_at) "
         "SELECT id, source, source_job_id, canonical_url, COALESCE(first_seen_at, discovered_at), "
         "COALESCE(last_seen_at, discovered_at) FROM jobs",),
    ),
)
