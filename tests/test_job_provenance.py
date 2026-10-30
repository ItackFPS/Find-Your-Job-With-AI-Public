"""Issue #86, slice 1: where each offer was seen (job_provenance).

* ``Storage.record_provenance`` writes one row per (offer, source), its own source
  with its id and URL, merged sources without; a later sighting moves only
  ``last_seen_at``.
* The ``backfill_job_provenance`` migration gives every stored offer its own source.
* A large database is copied before data migrations run; without room for the copy
  they wait.
* ``GET /api/jobs/{id}`` returns the provenance, own source first.

Temporary databases only.
"""

from __future__ import annotations

import sqlite3
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from src.storage import migrations
from src.storage.sqlite import Storage

T0 = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


def _storage(path: Path) -> Storage:
    Storage._initialized_paths.discard(str(path.resolve()))
    s = Storage(db_path=str(path))
    s.init_db()
    return s


def _job(job_id: str, source: str, sources: list[str]) -> Job:
    return Job(id=job_id, source=source, source_job_id=f"{source}-1", title="Développeur Python",
               canonical_url=f"https://{source}.example/offre/1", sources=sources)


def test_a_merged_offer_is_recorded_once_per_source(tmp_path: Path) -> None:
    s = _storage(tmp_path / "jobs.db")
    job = _job("j1", "france-travail", ["france-travail", "france-travail-web", "eures"])
    s.upsert_job(job)
    assert s.record_provenance([job], T0) == 3
    rows = s.get_provenance(["j1"])["j1"]
    assert [r["source"] for r in rows][0] == "france-travail"
    assert {r["source"] for r in rows} == {"france-travail", "france-travail-web", "eures"}
    own = rows[0]
    assert own["url"] == "https://france-travail.example/offre/1"
    assert all(r["url"] is None for r in rows[1:])

    later = T0 + timedelta(days=1)
    s.record_provenance([_job("j1", "france-travail", ["france-travail"])], later)
    again = {r["source"]: r for r in s.get_provenance(["j1"])["j1"]}
    assert again["france-travail"]["first_seen_at"] == T0.isoformat()
    assert again["france-travail"]["last_seen_at"] == later.isoformat()
    assert again["eures"]["last_seen_at"] == T0.isoformat()
    s.close()


def test_existing_offers_get_their_own_source_as_provenance(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    s = _storage(db)
    s.upsert_job(_job("j1", "eures", []))
    s.close()
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM job_provenance")
    conn.execute("DELETE FROM db_migrations WHERE name = 'backfill_job_provenance'")
    conn.commit()
    conn.close()

    s = _storage(db)
    rows = s.get_provenance(["j1"])["j1"]
    assert [(r["source"], r["url"]) for r in rows] == [("eures", "https://eures.example/offre/1")]
    s.close()


def _pending_db(path: Path) -> None:
    """A database with the provenance backfill still to run."""
    s = _storage(path)
    s.close()
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM db_migrations WHERE name = 'backfill_job_provenance'")
    conn.commit()
    conn.close()


def _applied(path: Path) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT 1 FROM db_migrations WHERE name = 'backfill_job_provenance'").fetchone() is not None
    finally:
        conn.close()


def test_a_large_database_is_backed_up_before_migrating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(migrations, "BACKUP_MIN_BYTES", 1)
    for _ in range(3):
        _pending_db(db)
        _storage(db).close()
        assert _applied(db)
    backups = sorted((tmp_path / "backups").glob("jobs-*.db"))
    assert 1 <= len(backups) <= migrations.BACKUPS_KEPT
    conn = sqlite3.connect(str(backups[-1]))
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'jobs'").fetchone()
    conn.close()


def test_without_room_for_the_backup_migrations_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "jobs.db"
    _pending_db(db)
    monkeypatch.setattr(migrations, "BACKUP_MIN_BYTES", 1)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(migrations.shutil, "disk_usage", lambda _p: usage(10, 10, 0))
    _storage(db).close()
    assert not _applied(db)
    assert not list((tmp_path / "backups").glob("*.db"))


def test_a_small_database_is_not_copied(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _pending_db(db)
    _storage(db).close()
    assert _applied(db)
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").glob("*.db"))


def test_job_detail_lists_where_the_offer_was_seen(isolated_app, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    db = Path(isolated_app.state.db_path)
    s = _storage(db)
    job = _job("j1", "france-travail-web", ["france-travail-web", "eures"])
    s.upsert_job(job)
    s.record_provenance([job], T0)
    s.close()

    body = TestClient(isolated_app).get("/api/jobs/j1").json()
    assert [p["source"] for p in body["provenance"]] == ["france-travail-web", "eures"]
    assert body["provenance"][0]["url"] == "https://france-travail-web.example/offre/1"
