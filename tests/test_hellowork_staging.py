"""Tests for HelloWorkStagingStorage, job_to_staging_record, and staging_row_to_job.

All tests use isolated temporary databases — the real data/jobs.db is never
touched. Mirrors tests/test_linkedin_staging.py's conventions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def staging(tmp_db: str):
    from src.hellowork.staging import HelloWorkStagingStorage
    store = HelloWorkStagingStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    source_job_id: str = "83072305",
    title: str = "Développeur Python",
    company: str = "Acme",
    description: str | None = "A great job",
    scan_run_id: str = "scan-001",
) -> dict[str, Any]:
    import hashlib
    import json
    now = datetime.now(UTC).isoformat()
    stable_id = hashlib.sha256(f"hellowork:{source_job_id}".encode()).hexdigest()[:32]
    return {
        "id": stable_id,
        "source_job_id": source_job_id,
        "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{source_job_id}.html",
        "title": title,
        "company": company,
        "location": "Paris, France",
        "countries": json.dumps(["FR"]),
        "is_remote": 0,
        "work_mode": "on_site",
        "employment_type": "full-time",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "EUR",
        "description": description,
        "published_at": None,
        "raw_payload": "{}",
        "scan_run_id": scan_run_id,
        "discovered_at": now,
        "last_seen_at": now,
    }


# ---------------------------------------------------------------------------
# insert_job
# ---------------------------------------------------------------------------

class TestInsertJob:
    def test_new_job_returns_true(self, staging) -> None:
        assert staging.insert_job(_make_record()) is True

    def test_duplicate_does_not_create_second_row(self, staging) -> None:
        record = _make_record()
        staging.insert_job(record)
        staging.insert_job(record)
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM hellowork_staging"
        ).fetchone()[0]
        assert count == 1

    def test_duplicate_returns_false(self, staging) -> None:
        record = _make_record()
        staging.insert_job(record)
        assert staging.insert_job(record) is False

    def test_new_job_has_waiting_status(self, staging) -> None:
        record = _make_record()
        staging.insert_job(record)
        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        assert row["status"] == "waiting"

    def test_refreshes_content_while_waiting(self, staging) -> None:
        record = _make_record(title="Old Title")
        staging.insert_job(record)
        updated = _make_record(title="New Title")
        staging.insert_job(updated)
        row = staging._get_conn().execute(
            "SELECT title FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        assert row["title"] == "New Title"

    def test_terminal_status_content_not_overwritten(self, staging) -> None:
        record = _make_record(title="Original Title")
        staging.insert_job(record)
        staging._get_conn().execute(
            "UPDATE hellowork_staging SET status = 'transferred' WHERE id = ?", (record["id"],)
        )
        staging._get_conn().commit()

        updated = _make_record(title="Should Not Apply")
        staging.insert_job(updated)
        row = staging._get_conn().execute(
            "SELECT title, status FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        assert row["title"] == "Original Title"
        assert row["status"] == "transferred"


# ---------------------------------------------------------------------------
# snapshot_waiting / mark_transferred_by_stable_ids / mark_remaining_as_processed / reset_snapshot
# ---------------------------------------------------------------------------

class TestSnapshotLifecycle:
    def test_snapshot_waiting_locks_rows(self, staging) -> None:
        r1, r2 = _make_record("1"), _make_record("2")
        staging.insert_job(r1)
        staging.insert_job(r2)
        snapshot = staging.snapshot_waiting("run-1")
        assert len(snapshot) == 2
        assert all(row["status"] == "snapshotted" for row in snapshot)
        assert all(row["snapshot_run_id"] == "run-1" for row in snapshot)

    def test_snapshot_excludes_already_snapshotted(self, staging) -> None:
        staging.insert_job(_make_record("1"))
        staging.snapshot_waiting("run-1")
        staging.insert_job(_make_record("2"))
        snapshot2 = staging.snapshot_waiting("run-2")
        assert len(snapshot2) == 1

    def test_mark_transferred_by_stable_ids(self, staging) -> None:
        record = _make_record("1")
        staging.insert_job(record)
        staging.snapshot_waiting("run-1")
        count = staging.mark_transferred_by_stable_ids("run-1", {record["id"]})
        assert count == 1
        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        assert row["status"] == "transferred"

    def test_mark_remaining_as_processed(self, staging) -> None:
        r1, r2 = _make_record("1"), _make_record("2")
        staging.insert_job(r1)
        staging.insert_job(r2)
        staging.snapshot_waiting("run-1")
        staging.mark_transferred_by_stable_ids("run-1", {r1["id"]})
        count = staging.mark_remaining_as_processed("run-1")
        assert count == 1
        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_staging WHERE id = ?", (r2["id"],)
        ).fetchone()
        assert row["status"] == "processed"

    def test_reset_snapshot_on_failure(self, staging) -> None:
        record = _make_record("1")
        staging.insert_job(record)
        staging.snapshot_waiting("run-1")
        count = staging.reset_snapshot("run-1")
        assert count == 1
        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        assert row["status"] == "waiting"
        assert row["snapshot_run_id"] is None

    def test_mark_transferred_empty_set_is_noop(self, staging) -> None:
        assert staging.mark_transferred_by_stable_ids("run-1", set()) == 0


# ---------------------------------------------------------------------------
# startup recovery
# ---------------------------------------------------------------------------

class TestStartupRecovery:
    def test_recover_stale_scans_marks_failed(self, staging) -> None:
        staging.create_scan_run("scan-1")
        count = staging.recover_stale_scans()
        assert count == 1
        rows = staging.list_scan_runs()
        assert rows[0]["status"] == "failed"

    def test_reset_stale_snapshots(self, staging) -> None:
        record = _make_record("1")
        staging.insert_job(record)
        staging.snapshot_waiting("run-1")
        count = staging.reset_stale_snapshots()
        assert count == 1
        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        assert row["status"] == "waiting"

    def test_interrupted_scan_preserves_already_staged_jobs(self, staging) -> None:
        """End-to-end checkpoint-recovery regression, mirroring
        tests/test_linkedin_staging.py's equivalent: a process interruption
        mid-scan must not lose jobs already staged — recover_stale_scans()
        only marks the run row failed, it never touches 'waiting' rows.
        """
        staging.create_scan_run("scan-interrupted")
        staging.insert_job(_make_record("100", scan_run_id="scan-interrupted"))
        staging.insert_job(_make_record("200", scan_run_id="scan-interrupted"))

        recovered = staging.recover_stale_scans()
        assert recovered == 1

        waiting = staging._get_conn().execute(
            "SELECT COUNT(*) FROM hellowork_staging WHERE status = 'waiting'"
        ).fetchone()[0]
        assert waiting == 2

        snapshot = staging.snapshot_waiting("run-after-restart")
        assert len(snapshot) == 2


# ---------------------------------------------------------------------------
# init_db() migration guard — see the matching test in
# tests/test_linkedin_staging.py for the full rationale (a fresh instance is
# constructed and init_db()'d on every pipeline run).
# ---------------------------------------------------------------------------

class TestInitDbMigrationGuard:
    def test_fresh_instance_same_db_path_skips_schema_setup(self, tmp_db: str) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage

        first = HelloWorkStagingStorage(db_path=tmp_db)
        first.init_db()
        first.close()

        second = HelloWorkStagingStorage(db_path=tmp_db)
        conn = second._get_conn()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        second.init_db()
        conn.set_trace_callback(None)

        assert statements == []
        second.close()


# ---------------------------------------------------------------------------
# Resumable scan checkpoint
# ---------------------------------------------------------------------------

class TestScanCheckpoint:
    def test_no_checkpoint_initially(self, staging) -> None:
        assert staging.get_scan_checkpoint() is None

    def test_save_and_get_checkpoint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["developpeur", "devops"])
        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["scan_id"] == "scan-1"
        assert json.loads(cp["completed_slugs_json"]) == ["developpeur", "devops"]

    def test_save_overwrites_previous_checkpoint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["developpeur"])
        staging.save_scan_checkpoint("scan-1", ["developpeur", "devops"])
        cp = staging.get_scan_checkpoint()
        assert json.loads(cp["completed_slugs_json"]) == ["developpeur", "devops"]
        # Still a single row — not accumulating history.
        row_count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM hellowork_scan_checkpoint"
        ).fetchone()[0]
        assert row_count == 1

    def test_clear_checkpoint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["developpeur"])
        staging.clear_scan_checkpoint()
        assert staging.get_scan_checkpoint() is None

    def test_clear_checkpoint_when_none_exists_is_a_noop(self, staging) -> None:
        staging.clear_scan_checkpoint()  # must not raise
        assert staging.get_scan_checkpoint() is None

    def test_checkpoint_not_reset_by_read_only_activity(self, staging) -> None:
        """Regression: normal API/dashboard-style reads (status queries,
        listing scan runs, constructing a fresh instance and re-running
        init_db()) must never reset or touch an existing checkpoint."""
        from src.hellowork.staging import HelloWorkStagingStorage

        staging.save_scan_checkpoint("scan-1", ["developpeur", "devops"])

        # Simulate a burst of read-only API-style activity.
        for _ in range(20):
            staging.get_status()
            staging.list_scan_runs()
            staging.count_by_status()
            fresh = HelloWorkStagingStorage(db_path=staging._db_path)
            fresh.init_db()
            fresh.get_scan_checkpoint()
            fresh.close()

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert json.loads(cp["completed_slugs_json"]) == ["developpeur", "devops"]


# ---------------------------------------------------------------------------
# scan_runs CRUD + status
# ---------------------------------------------------------------------------

class TestScanRuns:
    def test_complete_scan_run(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.complete_scan_run("scan-1", jobs_found=10, jobs_new=5, stats_json="{}")
        row = staging.list_scan_runs()[0]
        assert row["status"] == "completed"
        assert row["jobs_found"] == 10
        assert row["jobs_new"] == 5

    def test_fail_scan_run(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.fail_scan_run("scan-1", "boom")
        row = staging.list_scan_runs()[0]
        assert row["status"] == "failed"
        assert row["error"] == "boom"

    def test_get_status_waiting_count(self, staging) -> None:
        staging.insert_job(_make_record("1"))
        staging.insert_job(_make_record("2"))
        status = staging.get_status()
        assert status["waiting"] == 2

    def test_count_by_status(self, staging) -> None:
        staging.insert_job(_make_record("1"))
        staging.snapshot_waiting("run-1")
        counts = staging.count_by_status()
        assert counts == {"snapshotted": 1}


# ---------------------------------------------------------------------------
# job_to_staging_record / staging_row_to_job round trip
# ---------------------------------------------------------------------------

class TestConversion:
    def test_job_to_staging_record_round_trip(self, staging) -> None:
        from src.hellowork.staging import job_to_staging_record, staging_row_to_job
        from src.pipeline.normalize import normalize_job

        raw = {
            "title": "Développeur Python",
            "company": "Acme",
            "description": "Build things",
            "location": "Paris, France",
            "countries": ["FR"],
            "canonical_url": "https://www.hellowork.com/fr-fr/emplois/83072305.html",
            "source_job_id": "83072305",
            "published_at": "2026-09-01T00:00:00Z",
            "type": "full-time",
            "remote": False,
            "work_mode": None,
            "skills": [],
        }
        job = normalize_job("hellowork", raw)
        record = job_to_staging_record(job, "scan-1")
        assert staging.insert_job(record) is True

        snapshot = staging.snapshot_waiting("run-1")
        assert len(snapshot) == 1
        rebuilt = staging_row_to_job(snapshot[0])
        assert rebuilt.id == job.id
        assert rebuilt.title == job.title
        assert rebuilt.company == job.company
        assert rebuilt.source == "hellowork"
        assert rebuilt.canonical_url == job.canonical_url


# ---------------------------------------------------------------------------
# Pause/resume checkpoint state (issue #81)
# ---------------------------------------------------------------------------

class TestCheckpointLifecycleState:
    def test_save_defaults_to_running_and_stores_fingerprint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["dev"], fingerprint="fp-1")
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "running"
        assert cp["config_fingerprint"] == "fp-1"

    def test_save_without_fingerprint_keeps_the_stored_one(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["dev"], fingerprint="fp-1")
        staging.save_scan_checkpoint("scan-1", ["dev", "data"])
        assert staging.get_scan_checkpoint()["config_fingerprint"] == "fp-1"

    def test_set_state_keeps_progress(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["dev", "data"])
        staging.set_checkpoint_state("scan-1", "paused")
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert json.loads(cp["completed_slugs_json"]) == ["dev", "data"]

    def test_set_state_on_empty_table_creates_an_empty_progress_row(self, staging) -> None:
        staging.set_checkpoint_state("scan-1", "paused")
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert json.loads(cp["completed_slugs_json"]) == []

    def test_recover_marks_running_checkpoint_interrupted(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.save_scan_checkpoint("scan-1", ["dev"])
        staging.recover_stale_scans()
        assert staging.get_scan_checkpoint()["state"] == "interrupted"

    def test_recover_leaves_paused_checkpoint_and_run_alone(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.save_scan_checkpoint("scan-1", ["dev"])
        staging.set_checkpoint_state("scan-1", "paused")
        staging.pause_scan_run("scan-1")
        assert staging.recover_stale_scans() == 0
        assert staging.get_scan_checkpoint()["state"] == "paused"
        assert staging.list_scan_runs()[0]["status"] == "paused"

    def test_legacy_checkpoint_table_is_migrated_without_losing_the_row(
        self, tmp_db: str
    ) -> None:
        import sqlite3

        from src.hellowork.staging import HelloWorkStagingStorage

        raw = sqlite3.connect(tmp_db)
        raw.execute(
            """CREATE TABLE hellowork_scan_checkpoint (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   scan_id TEXT NOT NULL,
                   completed_slugs_json TEXT NOT NULL DEFAULT '[]',
                   updated_at TEXT NOT NULL)"""
        )
        raw.execute(
            "INSERT INTO hellowork_scan_checkpoint VALUES (1, 'old', '[\"dev\"]', '2026-01-01')"
        )
        raw.commit()
        raw.close()

        HelloWorkStagingStorage._initialized_paths.clear()
        store = HelloWorkStagingStorage(db_path=tmp_db)
        store.init_db()
        cp = store.get_scan_checkpoint()
        assert json.loads(cp["completed_slugs_json"]) == ["dev"]
        assert cp["state"] is None
        assert cp["config_fingerprint"] is None
        store.close()


def test_state_only_update_records_the_owning_scan(staging) -> None:
    staging.save_scan_checkpoint("old-scan", ["dev"])
    staging.set_checkpoint_state("resumed-scan", "running", create_if_missing=False)
    cp = staging.get_scan_checkpoint()
    assert cp["scan_id"] == "resumed-scan"
    assert json.loads(cp["completed_slugs_json"]) == ["dev"]


def test_column_migration_tolerates_a_column_added_concurrently(tmp_db: str) -> None:
    """Two processes can both see the columns missing on a not-yet-migrated
    database; the one that ALTERs second must not fail init_db (issue #81
    review, F6)."""
    import sqlite3

    from src.hellowork.staging import HelloWorkStagingStorage

    first = HelloWorkStagingStorage(db_path=tmp_db)
    first.init_db()  # the "other process" already added the columns
    first.close()

    class _StalePragmaConn:
        """Delegates to a real connection but reports the columns as missing."""

        def __init__(self, real: sqlite3.Connection) -> None:
            self.real = real

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA table_info(hellowork_scan_checkpoint)"):
                return iter([])
            return self.real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.real, name)

    HelloWorkStagingStorage._initialized_paths.clear()
    second = HelloWorkStagingStorage(db_path=tmp_db)
    real = second._get_conn()
    second._conn = _StalePragmaConn(real)  # type: ignore[assignment]
    try:
        second.init_db()  # must not raise "duplicate column name"
    finally:
        second._conn = real
    cols = {r[1] for r in real.execute("PRAGMA table_info(hellowork_scan_checkpoint)")}
    assert {"state", "config_fingerprint"} <= cols
    second.close()


def test_column_migration_propagates_errors_other_than_a_duplicate_column(
    tmp_db: str,
) -> None:
    """Only the concurrent-add race is tolerated: a locked database during the
    ALTER must fail init_db, not leave the columns silently missing (issue #81
    re-review, R2)."""
    import sqlite3

    from src.hellowork.staging import HelloWorkStagingStorage

    raw = sqlite3.connect(tmp_db)
    raw.execute("""CREATE TABLE hellowork_scan_checkpoint (
        id INTEGER PRIMARY KEY CHECK (id = 1), scan_id TEXT NOT NULL,
        completed_slugs_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)""")
    raw.commit()
    raw.close()

    class _LockedAlterConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self.real = real

        def execute(self, sql, *args):
            if sql.startswith("ALTER TABLE hellowork_scan_checkpoint"):
                raise sqlite3.OperationalError("database is locked")
            return self.real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.real, name)

    HelloWorkStagingStorage._initialized_paths.clear()
    store = HelloWorkStagingStorage(db_path=tmp_db)
    real = store._get_conn()
    store._conn = _LockedAlterConn(real)  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.init_db()
    finally:
        store._conn = real
    # Not marked initialised, so the next init_db retries the migration.
    store.init_db()
    cols = {r[1] for r in real.execute("PRAGMA table_info(hellowork_scan_checkpoint)")}
    assert {"state", "config_fingerprint"} <= cols
    store.close()
