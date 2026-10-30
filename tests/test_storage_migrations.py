"""Issue #84: database migrations in one place (src/storage/migrations.py).

* ``ensure_columns`` adds only the missing columns, in order, and is idempotent.
* ``apply_data_migrations`` runs each named migration once, in order, and records it.
* ``jobs.db`` from before the added columns is upgraded exactly as before the
  refactor: the columns are added, the two recorded one-shot migrations run once,
  and a database that already recorded them runs nothing again.

Temporary databases only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.storage.migrations import (
    JOBS_COLUMNS,
    JOBS_DATA_MIGRATIONS,
    DataMigration,
    apply_data_migrations,
    ensure_columns,
)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_ensure_columns_adds_only_missing_ones_in_order() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, b TEXT)")
    assert ensure_columns(conn, "t", {"a": "TEXT", "b": "TEXT", "c": "INTEGER NOT NULL DEFAULT 0"}) == ["a", "c"]
    assert _columns(conn, "t") == ["id", "b", "a", "c"]
    assert ensure_columns(conn, "t", {"a": "TEXT", "c": "INTEGER NOT NULL DEFAULT 0"}) == []


def test_data_migrations_run_once_in_order_and_are_recorded() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE log (step TEXT)")
    migrations = (
        DataMigration("first", ("INSERT INTO log VALUES ('first')",)),
        DataMigration("second", ("INSERT INTO log VALUES ('second-a')", "INSERT INTO log VALUES ('second-b')")),
    )
    assert apply_data_migrations(conn, migrations) == ["first", "second"]
    assert apply_data_migrations(conn, migrations) == []
    assert [r[0] for r in conn.execute("SELECT step FROM log")] == ["first", "second-a", "second-b"]
    assert {r[0] for r in conn.execute("SELECT name FROM db_migrations")} == {"first", "second"}


def test_migration_names_are_unique() -> None:
    names = [m.name for m in JOBS_DATA_MIGRATIONS]
    assert len(names) == len(set(names))
    # Renaming one would run it again on every existing database.
    assert names[:2] == ["purge_jobs_without_canonical_url", "normalize_linkedin_fr_to_france"]


def _old_jobs_db(path: Path) -> None:
    """A jobs.db as it was before the added columns and the migrations table."""
    from src.storage.sqlite import Storage

    Storage(db_path=str(path)).init_db()
    Storage._initialized_paths.discard(str(path.resolve()))
    conn = sqlite3.connect(str(path))
    for column in JOBS_COLUMNS:
        conn.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
    conn.execute("DROP TABLE db_migrations")
    for job_id, source, countries in (("j1", "linkedin", '["FR"]'), ("j2", "eures", '["FR"]')):
        conn.execute(
            "INSERT INTO jobs (id, source, title, countries, content_hash, discovered_at) VALUES (?, ?, 'Dev', ?, '', '2026-10-01')",
            (job_id, source, countries),
        )
    conn.commit()
    conn.close()


def _init(path: Path) -> sqlite3.Connection:
    from src.storage.sqlite import Storage

    Storage._initialized_paths.discard(str(path.resolve()))
    storage = Storage(db_path=str(path))
    storage.init_db()
    storage.close()
    return sqlite3.connect(str(path))


def test_an_old_jobs_db_is_upgraded_as_before(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _old_jobs_db(db)
    conn = _init(db)
    assert set(JOBS_COLUMNS) <= set(_columns(conn, "jobs"))
    assert {r[0] for r in conn.execute("SELECT name FROM db_migrations")} == {m.name for m in JOBS_DATA_MIGRATIONS}
    # The purge kept only EURES (it never has a URL); the LinkedIn row had none.
    assert [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY id")] == ["j2"]
    conn.close()


def test_recorded_migrations_do_not_run_again(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    conn = _init(db)
    conn.execute(
        "INSERT INTO jobs (id, source, title, countries, content_hash, discovered_at) "
        "VALUES ('j3', 'linkedin', 'Dev', '[\"FR\"]', '', '2026-10-01')"
    )
    conn.commit()
    conn.close()

    conn = _init(db)
    assert conn.execute("SELECT countries FROM jobs WHERE id = 'j3'").fetchone() == ('["FR"]',)
    conn.close()
