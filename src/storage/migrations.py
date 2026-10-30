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

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


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
}

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
)
