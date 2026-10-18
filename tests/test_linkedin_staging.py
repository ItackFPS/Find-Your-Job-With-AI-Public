"""Tests for LinkedInStagingStorage and staging_row_to_job.

All tests use isolated temporary databases — the real data/jobs.db is never
touched.  Concurrency scenarios are verified with real threads.
"""

from __future__ import annotations

import json
import threading
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
    from src.linkedin.staging import LinkedInStagingStorage
    store = LinkedInStagingStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    linkedin_job_id: str = "1234567890",
    title: str = "Software Engineer",
    company: str = "Acme",
    description: str | None = "A great job",
    scan_run_id: str = "scan-001",
    discovered_by_query: str | None = None,
) -> dict[str, Any]:
    import hashlib
    import json
    now = datetime.now(UTC).isoformat()
    stable_id = hashlib.sha256(f"linkedin:{linkedin_job_id}".encode()).hexdigest()[:32]
    record: dict[str, Any] = {
        "id": stable_id,
        "linkedin_job_id": linkedin_job_id,
        "canonical_url": f"https://www.linkedin.com/jobs/view/{linkedin_job_id}",
        "title": title,
        "company": company,
        "location": "Paris, France",
        "countries": json.dumps(["FR"]),
        "is_remote": 0,
        "work_mode": "on_site",
        "employment_type": "full_time",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "EUR",
        "description": description,
        "description_fetched": 1 if description else 0,
        "published_at": None,
        "raw_payload": "{}",
        "scan_run_id": scan_run_id,
        "discovered_at": now,
        "last_seen_at": now,
    }
    if discovered_by_query is not None:
        record["discovered_by_query"] = discovered_by_query
    return record


# ---------------------------------------------------------------------------
# insert_job
# ---------------------------------------------------------------------------

class TestInsertJob:
    def test_new_job_returns_true(self, staging) -> None:
        record = _make_record()
        assert staging.insert_job(record) is True

    def test_duplicate_does_not_create_second_row(self, staging) -> None:
        record = _make_record()
        staging.insert_job(record)
        staging.insert_job(record)
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1

    def test_duplicate_returns_false(self, staging) -> None:
        record = _make_record()
        staging.insert_job(record)
        assert staging.insert_job(record) is False

    def test_updates_last_seen_at_on_re_discovery(self, staging) -> None:
        import time as _time
        record = _make_record(description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)
        original_seen = staging._get_conn().execute(
            "SELECT last_seen_at FROM linkedin_staging WHERE linkedin_job_id = ?",
            (record["linkedin_job_id"],),
        ).fetchone()[0]

        _time.sleep(0.01)
        new_record = _make_record(description=None)
        new_record["last_seen_at"] = datetime.now(UTC).isoformat()
        new_record["description_fetched"] = 0
        staging.insert_job(new_record)

        updated_seen = staging._get_conn().execute(
            "SELECT last_seen_at FROM linkedin_staging WHERE linkedin_job_id = ?",
            (record["linkedin_job_id"],),
        ).fetchone()[0]
        assert updated_seen >= original_seen

    def test_updates_description_when_previously_missing(self, staging) -> None:
        record = _make_record(description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)

        record2 = _make_record(description="Full description now available")
        record2["description_fetched"] = 1
        staging.insert_job(record2)

        row = staging._get_conn().execute(
            "SELECT description, description_fetched FROM linkedin_staging WHERE linkedin_job_id = ?",
            (record["linkedin_job_id"],),
        ).fetchone()
        assert row["description"] == "Full description now available"
        assert row["description_fetched"] == 1

    def test_does_not_overwrite_existing_description(self, staging) -> None:
        record = _make_record(description="Original description")
        staging.insert_job(record)

        record2 = _make_record(description="New description attempt")
        staging.insert_job(record2)

        row = staging._get_conn().execute(
            "SELECT description FROM linkedin_staging WHERE linkedin_job_id = ?",
            (record["linkedin_job_id"],),
        ).fetchone()
        assert row["description"] == "Original description"

    def test_new_job_has_waiting_status(self, staging) -> None:
        record = _make_record()
        staging.insert_job(record)
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = ?",
            (record["linkedin_job_id"],),
        ).fetchone()
        assert row["status"] == "waiting"


# ---------------------------------------------------------------------------
# needs_description / stale-record rediscovery
# ---------------------------------------------------------------------------

class TestNeedsDescriptionAndStaleRediscovery:
    """Old LinkedIn records collected with no description (description_fetched=0)
    must remain eligible for rediscovery instead of permanently blocking the
    scanner's description enrichment just because the job is already known."""

    def test_needs_description_true_for_unknown_job(self, staging) -> None:
        assert staging.needs_description("no-such-job") is True

    def test_needs_description_true_when_fetch_never_succeeded(self, staging) -> None:
        record = _make_record("111", description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)
        assert staging.needs_description("111") is True

    def test_needs_description_false_when_description_present(self, staging) -> None:
        record = _make_record("111", description="A real description")
        staging.insert_job(record)
        assert staging.needs_description("111") is False

    def test_needs_description_false_when_permanently_unavailable(self, staging) -> None:
        record = _make_record("111", description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)
        staging._get_conn().execute(
            "UPDATE linkedin_staging SET description_fetched = 2 WHERE linkedin_job_id = '111'"
        )
        staging._get_conn().commit()
        assert staging.needs_description("111") is False

    def test_insert_job_marks_permanently_unavailable_via_description_fetched_2(self, staging) -> None:
        """insert_job() itself (not a raw SQL UPDATE) must apply the
        description_fetched: 0 -> 2 transition when the scanner classifies a
        re-discovered job's targeted fetch as HTTP 404/410 ('deleted') —
        this is the actual wiring path the scanner calls (see
        fetch_linkedin_description_classified in src/linkedin/scanner.py)."""
        record = _make_record("111", description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)
        assert staging.needs_description("111") is True

        rediscovered = _make_record("111", description=None)
        rediscovered["description_fetched"] = 2
        assert staging.insert_job(rediscovered) is False  # not a new row

        row = staging._get_conn().execute(
            "SELECT description, description_fetched FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["description"] is None
        assert row["description_fetched"] == 2
        assert staging.needs_description("111") is False

    def test_insert_job_never_downgrades_a_fetched_description_to_deleted(self, staging) -> None:
        """A job whose description was already successfully fetched must
        never be marked 'deleted' by a later re-discovery — the CASE only
        fires when the current row is still description_fetched=0."""
        record = _make_record("111", description="A real description")
        staging.insert_job(record)  # description_fetched=1 (see _make_record)

        rediscovered = _make_record("111", description=None)
        rediscovered["description_fetched"] = 2
        staging.insert_job(rediscovered)

        row = staging._get_conn().execute(
            "SELECT description, description_fetched FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["description"] == "A real description"
        assert row["description_fetched"] == 1

    def _stage_and_transfer_incomplete(self, staging, job_id: str, status: str) -> None:
        """Simulate an old job that went through the pipeline with no description."""
        record = _make_record(job_id, description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)
        staging.snapshot_waiting(f"old-run-{job_id}")
        if status == "transferred":
            staging.mark_transferred(f"old-run-{job_id}")
        elif status == "processed":
            staging.mark_remaining_as_processed(f"old-run-{job_id}")

    def test_rediscovery_reopens_stale_transferred_record(self, staging) -> None:
        self._stage_and_transfer_incomplete(staging, "111", "transferred")

        rediscovered = _make_record("111", description="Full description now available")
        rediscovered["description_fetched"] = 1
        assert staging.insert_job(rediscovered) is False  # not a new row

        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id, description, description_fetched "
            "FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "waiting"
        assert row["snapshot_run_id"] is None
        assert row["description"] == "Full description now available"
        assert row["description_fetched"] == 1

    def test_rediscovery_reopens_stale_processed_record(self, staging) -> None:
        self._stage_and_transfer_incomplete(staging, "222", "processed")

        rediscovered = _make_record("222", description="Full description now available")
        rediscovered["description_fetched"] = 1
        staging.insert_job(rediscovered)

        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id FROM linkedin_staging WHERE linkedin_job_id = '222'"
        ).fetchone()
        assert row["status"] == "waiting"
        assert row["snapshot_run_id"] is None

    def test_reopened_record_can_be_snapshotted_again(self, staging) -> None:
        """The reopened job must flow through the pipeline again like any other."""
        self._stage_and_transfer_incomplete(staging, "111", "transferred")
        rediscovered = _make_record("111", description="Full description now available")
        rediscovered["description_fetched"] = 1
        staging.insert_job(rediscovered)

        rows = staging.snapshot_waiting("new-run-001")
        assert len(rows) == 1
        assert rows[0]["linkedin_job_id"] == "111"
        assert rows[0]["description"] == "Full description now available"

    def test_rediscovery_without_new_description_does_not_reopen(self, staging) -> None:
        """A stale record seen again with STILL no description stays terminal —
        it must not be resurrected until a real description is actually found."""
        self._stage_and_transfer_incomplete(staging, "111", "transferred")

        still_missing = _make_record("111", description=None)
        still_missing["description_fetched"] = 0
        staging.insert_job(still_missing)

        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "transferred"

    def test_rediscovery_does_not_disturb_waiting_job(self, staging) -> None:
        """A job that's simply 'waiting' (never blocked) keeps its status as-is
        when its description arrives — no premature snapshot-run interference."""
        record = _make_record("111", description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)

        rediscovered = _make_record("111", description="Now has a description")
        rediscovered["description_fetched"] = 1
        staging.insert_job(rediscovered)

        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "waiting"
        assert row["snapshot_run_id"] is None

    def test_rediscovery_does_not_disturb_snapshotted_job(self, staging) -> None:
        """A job currently locked into an in-flight pipeline run must not be
        yanked back to 'waiting' out from under that run."""
        record = _make_record("111", description=None)
        record["description_fetched"] = 0
        staging.insert_job(record)
        staging.snapshot_waiting("run-in-flight")

        rediscovered = _make_record("111", description="Now has a description")
        rediscovered["description_fetched"] = 1
        staging.insert_job(rediscovered)

        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "snapshotted"
        assert row["snapshot_run_id"] == "run-in-flight"

    def test_reopening_never_creates_a_duplicate_row(self, staging) -> None:
        self._stage_and_transfer_incomplete(staging, "111", "transferred")

        rediscovered = _make_record("111", description="Full description now available")
        rediscovered["description_fetched"] = 1
        staging.insert_job(rediscovered)

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1

    def test_valid_existing_job_with_description_is_untouched_by_rescans(self, staging) -> None:
        """A job that already has a description must never be reset or rewritten,
        even if it is 'transferred' and gets seen again in a later scan."""
        record = _make_record("333", description="Original complete description")
        staging.insert_job(record)
        staging.snapshot_waiting("run-333")
        staging.mark_transferred("run-333")

        reseen = _make_record("333", description="Some other description text")
        reseen["description_fetched"] = 1
        staging.insert_job(reseen)

        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id, description FROM linkedin_staging "
            "WHERE linkedin_job_id = '333'"
        ).fetchone()
        assert row["status"] == "transferred"
        assert row["snapshot_run_id"] == "run-333"  # untouched, not reset
        assert row["description"] == "Original complete description"


# ---------------------------------------------------------------------------
# snapshot_waiting
# ---------------------------------------------------------------------------

class TestSnapshotWaiting:
    def test_empty_staging_returns_empty_list(self, staging) -> None:
        result = staging.snapshot_waiting("run-001")
        assert result == []

    def test_moves_waiting_to_snapshotted(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.insert_job(_make_record("222"))

        rows = staging.snapshot_waiting("run-001")
        assert len(rows) == 2
        for row in rows:
            assert row["status"] == "snapshotted"
            assert row["snapshot_run_id"] == "run-001"

    def test_second_snapshot_does_not_steal_first(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")  # locks job 111

        staging.insert_job(_make_record("222"))  # new job added
        rows2 = staging.snapshot_waiting("run-002")
        assert len(rows2) == 1
        assert rows2[0]["linkedin_job_id"] == "222"
        assert rows2[0]["snapshot_run_id"] == "run-002"

    def test_jobs_inserted_after_snapshot_not_included(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        rows = staging.snapshot_waiting("run-001")
        assert len(rows) == 1

        # Insert after snapshot; must NOT appear in run-001
        staging.insert_job(_make_record("222"))
        rows_after = staging._get_conn().execute(
            "SELECT * FROM linkedin_staging WHERE snapshot_run_id = 'run-001'"
        ).fetchall()
        assert len(rows_after) == 1
        assert rows_after[0]["linkedin_job_id"] == "111"

    def test_concurrent_insert_not_included_in_earlier_snapshot(self, tmp_db: str) -> None:
        """Scanner inserting concurrently with a snapshot must not corrupt state.

        Each thread uses its own LinkedInStagingStorage instance (the real pattern:
        the scanner runs in one thread with its own connection, the pipeline runner
        runs in another thread with its own connection, both sharing jobs.db via WAL).
        """
        from src.linkedin.staging import LinkedInStagingStorage

        # Seed: job 111 is already waiting
        setup = LinkedInStagingStorage(db_path=tmp_db)
        setup.init_db()
        setup.insert_job(_make_record("111"))
        setup.close()

        snap_result: list[list[Any]] = [[]]
        snap_done = threading.Event()

        def do_snapshot() -> None:
            s = LinkedInStagingStorage(db_path=tmp_db)
            s.init_db()
            snap_result[0] = s.snapshot_waiting("run-001")
            s.close()
            snap_done.set()

        def do_insert() -> None:
            # Simulate scanner inserting job 222 around the same time as snapshot
            s = LinkedInStagingStorage(db_path=tmp_db)
            s.init_db()
            s.insert_job(_make_record("222"))
            s.close()

        t1 = threading.Thread(target=do_snapshot)
        t2 = threading.Thread(target=do_insert)
        t1.start()
        t2.start()
        t1.join(timeout=3)
        t2.join(timeout=3)

        snap_done.wait(timeout=3)
        snapped = snap_result[0]

        # job 111 was waiting BEFORE both threads started — must always be snapshotted
        assert any(r["linkedin_job_id"] == "111" for r in snapped)
        assert all(r["snapshot_run_id"] == "run-001" for r in snapped)

        # Verify final DB state: no corruption regardless of which thread won the race
        verify = LinkedInStagingStorage(db_path=tmp_db)
        verify.init_db()
        conn = verify._get_conn()

        count = conn.execute("SELECT COUNT(*) FROM linkedin_staging").fetchone()[0]
        assert count == 2

        for row in conn.execute("SELECT linkedin_job_id, status, snapshot_run_id FROM linkedin_staging").fetchall():
            if row["linkedin_job_id"] == "111":
                assert row["status"] == "snapshotted"
                assert row["snapshot_run_id"] == "run-001"
            else:
                # job 222: either captured in the same snapshot or still waiting
                assert row["status"] in ("snapshotted", "waiting")
                if row["status"] == "snapshotted":
                    assert row["snapshot_run_id"] == "run-001"
                else:
                    assert row["snapshot_run_id"] is None

        verify.close()


# ---------------------------------------------------------------------------
# mark_transferred / reset_snapshot
# ---------------------------------------------------------------------------

class TestStateTransitions:
    def test_mark_transferred(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")
        staging.mark_transferred("run-001")
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "transferred"

    def test_reset_snapshot_returns_jobs_to_waiting(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")
        count = staging.reset_snapshot("run-001")
        assert count == 1
        row = staging._get_conn().execute(
            "SELECT status, snapshot_run_id FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "waiting"
        assert row["snapshot_run_id"] is None

    def test_reset_snapshot_does_not_affect_transferred(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")
        staging.mark_transferred("run-001")
        # A later reset call for a different run must not undo the transfer
        staging.reset_snapshot("run-002")
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "transferred"

    def test_pipeline_failure_makes_jobs_available_for_next_run(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")
        staging.reset_snapshot("run-001")  # simulate pipeline failure

        # Next run must be able to snapshot the job
        rows = staging.snapshot_waiting("run-002")
        assert len(rows) == 1
        assert rows[0]["linkedin_job_id"] == "111"
        assert rows[0]["snapshot_run_id"] == "run-002"

    def test_transferred_records_never_deleted(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")
        staging.mark_transferred("run-001")
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()[0]
        assert count == 1  # record still exists; status is 'transferred'


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

class TestStartupRecovery:
    def test_recover_stale_scans(self, staging) -> None:
        staging.create_scan_run("scan-001")
        staging.create_scan_run("scan-002")
        staging.complete_scan_run("scan-002", 5, 3)

        recovered = staging.recover_stale_scans()
        assert recovered == 1

        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = 'scan-001'"
        ).fetchone()
        assert row["status"] == "failed"

        row2 = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = 'scan-002'"
        ).fetchone()
        assert row2["status"] == "completed"

    def test_reset_stale_snapshots(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.insert_job(_make_record("222"))
        staging.snapshot_waiting("run-001")  # both snapshotted

        count = staging.reset_stale_snapshots()
        assert count == 2

        rows = staging._get_conn().execute(
            "SELECT status, snapshot_run_id FROM linkedin_staging"
        ).fetchall()
        for row in rows:
            assert row["status"] == "waiting"
            assert row["snapshot_run_id"] is None

    def test_reset_stale_snapshots_does_not_touch_transferred(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.snapshot_waiting("run-001")
        staging.mark_transferred("run-001")

        count = staging.reset_stale_snapshots()
        assert count == 0

        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = '111'"
        ).fetchone()
        assert row["status"] == "transferred"


# ---------------------------------------------------------------------------
# Scan run tracking
# ---------------------------------------------------------------------------

class TestScanRunCrud:
    def test_create_scan_run(self, staging) -> None:
        staging.create_scan_run("scan-001", '[{"search_term": "engineer"}]')
        row = staging._get_conn().execute(
            "SELECT * FROM linkedin_scan_runs WHERE id = 'scan-001'"
        ).fetchone()
        assert row["status"] == "running"
        assert row["jobs_found"] == 0

    def test_complete_scan_run(self, staging) -> None:
        staging.create_scan_run("scan-001")
        staging.complete_scan_run("scan-001", 42, 15)
        row = staging._get_conn().execute(
            "SELECT * FROM linkedin_scan_runs WHERE id = 'scan-001'"
        ).fetchone()
        assert row["status"] == "completed"
        assert row["jobs_found"] == 42
        assert row["jobs_new"] == 15
        assert row["completed_at"] is not None

    def test_fail_scan_run(self, staging) -> None:
        staging.create_scan_run("scan-001")
        staging.fail_scan_run("scan-001", "Connection refused")
        row = staging._get_conn().execute(
            "SELECT * FROM linkedin_scan_runs WHERE id = 'scan-001'"
        ).fetchone()
        assert row["status"] == "failed"
        assert "Connection refused" in row["error"]


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_empty_database(self, staging) -> None:
        status = staging.get_status()
        assert status["waiting"] == 0
        assert status["last_scan"] is None

    def test_counts_only_waiting(self, staging) -> None:
        staging.insert_job(_make_record("111"))
        staging.insert_job(_make_record("222"))
        staging.snapshot_waiting("run-001")
        staging.mark_transferred("run-001")
        staging.insert_job(_make_record("333"))  # new waiting job

        status = staging.get_status()
        assert status["waiting"] == 1

    def test_last_scan_present(self, staging) -> None:
        staging.create_scan_run("scan-001")
        staging.complete_scan_run("scan-001", 10, 5)
        status = staging.get_status()
        assert status["last_scan"] is not None
        assert status["last_scan"]["id"] == "scan-001"


# ---------------------------------------------------------------------------
# staging_row_to_job
# ---------------------------------------------------------------------------

class TestStagingRowToJob:
    def test_converts_to_job_with_stable_id(self, staging) -> None:
        import hashlib

        from src.linkedin.staging import staging_row_to_job

        record = _make_record("9999")
        staging.insert_job(record)
        rows = staging._get_conn().execute(
            "SELECT * FROM linkedin_staging WHERE linkedin_job_id = '9999'"
        ).fetchall()
        row = dict(rows[0])
        job = staging_row_to_job(row)

        expected_id = hashlib.sha256(b"linkedin:9999").hexdigest()[:32]
        assert job.id == expected_id
        assert job.source == "linkedin"
        assert job.source_job_id == "9999"
        assert job.canonical_url == "https://www.linkedin.com/jobs/view/9999"

    def test_job_title_and_company(self, staging) -> None:
        from src.linkedin.staging import staging_row_to_job
        record = _make_record("8888", title="Backend Engineer", company="Stripe")
        staging.insert_job(record)
        row = dict(staging._get_conn().execute(
            "SELECT * FROM linkedin_staging WHERE linkedin_job_id = '8888'"
        ).fetchone())
        job = staging_row_to_job(row)
        assert job.title == "Backend Engineer"
        assert job.company == "Stripe"

    def test_job_countries_deserialized(self, staging) -> None:
        """staging_row_to_job normalizes ISO codes to full country names."""
        from src.linkedin.staging import staging_row_to_job
        record = _make_record("7777")  # inserts countries=["FR"]
        staging.insert_job(record)
        row = dict(staging._get_conn().execute(
            "SELECT * FROM linkedin_staging WHERE linkedin_job_id = '7777'"
        ).fetchone())
        job = staging_row_to_job(row)
        # FR → France after normalization
        assert "France" in job.countries

    def test_content_hash_computed(self, staging) -> None:
        from src.linkedin.staging import staging_row_to_job
        record = _make_record("6666")
        staging.insert_job(record)
        row = dict(staging._get_conn().execute(
            "SELECT * FROM linkedin_staging WHERE linkedin_job_id = '6666'"
        ).fetchone())
        job = staging_row_to_job(row)
        assert job.content_hash != ""


# ---------------------------------------------------------------------------
# Query attribution
# ---------------------------------------------------------------------------

class TestQueryAttribution:
    def test_insert_job_with_discovered_by_query(self, staging) -> None:
        import json as _json
        record = _make_record("attr0001", discovered_by_query="python developer")
        staging.insert_job(record)
        row = staging._get_conn().execute(
            "SELECT discovered_by_query, seen_by_queries FROM linkedin_staging "
            "WHERE linkedin_job_id = 'attr0001'"
        ).fetchone()
        assert row["discovered_by_query"] == "python developer"
        assert _json.loads(row["seen_by_queries"]) == ["python developer"]

    def test_insert_job_without_discovered_by_query_defaults(self, staging) -> None:
        import json as _json
        record = _make_record("attr0002")
        staging.insert_job(record)
        row = staging._get_conn().execute(
            "SELECT discovered_by_query, seen_by_queries FROM linkedin_staging "
            "WHERE linkedin_job_id = 'attr0002'"
        ).fetchone()
        assert row["discovered_by_query"] is None
        assert _json.loads(row["seen_by_queries"]) == []

    def test_record_also_seen_by_appends_query(self, staging) -> None:
        import json as _json
        record = _make_record("attr0003", discovered_by_query="backend developer", scan_run_id="scan-A")
        staging.insert_job(record)
        staging.record_also_seen_by("attr0003", "software engineer", "scan-A")
        row = staging._get_conn().execute(
            "SELECT seen_by_queries FROM linkedin_staging WHERE linkedin_job_id = 'attr0003'"
        ).fetchone()
        seen = _json.loads(row["seen_by_queries"])
        assert "backend developer" in seen
        assert "software engineer" in seen
        assert len(seen) == 2

    def test_record_also_seen_by_is_idempotent(self, staging) -> None:
        import json as _json
        record = _make_record("attr0004", discovered_by_query="backend developer", scan_run_id="scan-A")
        staging.insert_job(record)
        staging.record_also_seen_by("attr0004", "software engineer", "scan-A")
        staging.record_also_seen_by("attr0004", "software engineer", "scan-A")
        row = staging._get_conn().execute(
            "SELECT seen_by_queries FROM linkedin_staging WHERE linkedin_job_id = 'attr0004'"
        ).fetchone()
        seen = _json.loads(row["seen_by_queries"])
        assert seen.count("software engineer") == 1

    def test_record_also_seen_by_noop_for_different_scan(self, staging) -> None:
        import json as _json
        record = _make_record("attr0005", discovered_by_query="python developer", scan_run_id="scan-A")
        staging.insert_job(record)
        # Wrong scan_run_id — must not affect the row
        staging.record_also_seen_by("attr0005", "data engineer", "scan-B")
        row = staging._get_conn().execute(
            "SELECT seen_by_queries FROM linkedin_staging WHERE linkedin_job_id = 'attr0005'"
        ).fetchone()
        seen = _json.loads(row["seen_by_queries"])
        assert seen == ["python developer"]

    def test_record_also_seen_by_noop_for_unknown_job(self, staging) -> None:
        # Should not raise, just silently do nothing
        staging.record_also_seen_by("nonexistent", "some query", "scan-A")

    def test_count_unique_to_query_single_query(self, staging) -> None:
        staging.create_scan_run("scan-U1")
        staging.insert_job(_make_record("u001", discovered_by_query="python developer", scan_run_id="scan-U1"))
        staging.insert_job(_make_record("u002", discovered_by_query="python developer", scan_run_id="scan-U1"))
        count = staging.count_unique_to_query("scan-U1", "python developer")
        assert count == 2

    def test_count_unique_to_query_excludes_cross_query_dups(self, staging) -> None:
        staging.create_scan_run("scan-U2")
        staging.insert_job(_make_record("u003", discovered_by_query="python developer", scan_run_id="scan-U2"))
        staging.insert_job(_make_record("u004", discovered_by_query="python developer", scan_run_id="scan-U2"))
        # u003 was also seen by a second query
        staging.record_also_seen_by("u003", "backend developer", "scan-U2")
        # u003 is no longer unique to python developer; u004 still is
        count = staging.count_unique_to_query("scan-U2", "python developer")
        assert count == 1

    def test_count_unique_to_query_zero_for_other_scan(self, staging) -> None:
        staging.create_scan_run("scan-U3")
        staging.insert_job(_make_record("u005", discovered_by_query="python developer", scan_run_id="scan-U3"))
        # Asking about a different scan → 0
        count = staging.count_unique_to_query("scan-other", "python developer")
        assert count == 0


# ---------------------------------------------------------------------------
# init_db() migration guard
#
# A fresh LinkedInStagingStorage is constructed and init_db()'d on every
# pipeline run (src/api/staged_sources.py::LinkedInStagedSourceAdapter).
# Without a per-process guard, init_db()'s full-table UPDATE backfills
# (salary_currency, work_mode) re-ran on every single call, holding the
# writer lock against the scanner's concurrent inserts and surfacing as
# "database is locked". init_db() must now run its migrations only once per
# resolved db path per process, no matter how many instances call it.
# ---------------------------------------------------------------------------

class TestInitDbMigrationGuard:
    def test_second_init_db_call_skips_migration_writes(self, tmp_db: str) -> None:
        from src.linkedin.staging import LinkedInStagingStorage

        store = LinkedInStagingStorage(db_path=tmp_db)
        store.init_db()

        conn = store._get_conn()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        store.init_db()  # second call on the SAME instance
        conn.set_trace_callback(None)

        assert statements == []  # no SQL executed at all — guard returned early

    def test_fresh_instance_same_db_path_also_skips_migration(self, tmp_db: str) -> None:
        """The guard is keyed by resolved db path, not by instance — a brand
        new LinkedInStagingStorage pointed at an already-initialised path
        (the real-world case: a new instance per pipeline run) must not
        re-run the migration either."""
        from src.linkedin.staging import LinkedInStagingStorage

        first = LinkedInStagingStorage(db_path=tmp_db)
        first.init_db()
        first.close()

        second = LinkedInStagingStorage(db_path=tmp_db)
        conn = second._get_conn()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        second.init_db()
        conn.set_trace_callback(None)

        assert statements == []
        second.close()


# ---------------------------------------------------------------------------
# Checkpoint recovery: interrupted scan preserves already-staged jobs
#
# End-to-end regression for "recover from the latest valid checkpoint
# instead of restarting from zero": a process interruption mid-scan (crash,
# restart) must not lose jobs already staged, and startup recovery must not
# duplicate or corrupt them — the next scan (and the next pipeline run)
# simply picks up where the waiting queue actually is.
# ---------------------------------------------------------------------------

class TestCheckpointRecoveryAfterInterruption:
    def test_interrupted_scan_preserves_already_staged_jobs(self, staging) -> None:
        # Scan #1 discovers two jobs, then is interrupted (process dies) —
        # its scan_run row is left 'running' and never completed.
        staging.create_scan_run("scan-interrupted")
        staging.insert_job(_make_record("100", scan_run_id="scan-interrupted"))
        staging.insert_job(_make_record("200", scan_run_id="scan-interrupted"))

        # Simulate the app restarting: startup recovery runs.
        recovered = staging.recover_stale_scans()
        assert recovered == 1
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = 'scan-interrupted'"
        ).fetchone()
        assert row["status"] == "failed"

        # The two already-discovered jobs must still be there, untouched and
        # still waiting — nothing was lost or reset to zero.
        waiting = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE status = 'waiting'"
        ).fetchone()[0]
        assert waiting == 2

        # A fresh pipeline run can snapshot them normally.
        snapshot = staging.snapshot_waiting("run-after-restart")
        assert len(snapshot) == 2

    def test_rediscovering_a_job_from_an_interrupted_scan_does_not_duplicate_it(
        self, staging
    ) -> None:
        staging.create_scan_run("scan-interrupted-2")
        staging.insert_job(_make_record("300", scan_run_id="scan-interrupted-2"))
        staging.recover_stale_scans()  # process "restarts"

        # Even if a job is re-discovered anyway (e.g. the resumed scan's
        # first fetched page overlaps the last completed one, or the
        # scan_checkpoint itself wasn't set for whatever reason) —
        # insert_job's upsert-on-conflict must treat it as a re-sighting,
        # not a new row. See TestScanCheckpoint below for the page-level
        # resume mechanism that makes this overlap the exception rather
        # than the rule.
        staging.create_scan_run("scan-after-restart")
        is_new = staging.insert_job(_make_record("300", scan_run_id="scan-after-restart"))
        assert is_new is False

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE linkedin_job_id = '300'"
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Resumable scan checkpoint (page/query-level cursor)
# ---------------------------------------------------------------------------

class TestScanCheckpoint:
    def test_no_checkpoint_initially(self, staging) -> None:
        assert staging.get_scan_checkpoint() is None

    def test_save_and_get_checkpoint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["term-a\x1floc-a"], "term-b\x1floc-b", 3)
        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["scan_id"] == "scan-1"
        assert json.loads(cp["completed_queries_json"]) == ["term-a\x1floc-a"]
        assert cp["current_query_key"] == "term-b\x1floc-b"
        assert cp["current_page"] == 3

    def test_save_overwrites_previous_checkpoint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 1)
        staging.save_scan_checkpoint("scan-1", ["term-a\x1floc-a"], "term-b\x1floc-b", 0)
        cp = staging.get_scan_checkpoint()
        assert json.loads(cp["completed_queries_json"]) == ["term-a\x1floc-a"]
        assert cp["current_query_key"] == "term-b\x1floc-b"
        assert cp["current_page"] == 0
        row_count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_scan_checkpoint"
        ).fetchone()[0]
        assert row_count == 1

    def test_current_query_key_can_be_cleared_to_null(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 2)
        staging.save_scan_checkpoint("scan-1", ["term-a\x1floc-a"], None, 0)
        cp = staging.get_scan_checkpoint()
        assert cp["current_query_key"] is None

    def test_clear_checkpoint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 1)
        staging.clear_scan_checkpoint()
        assert staging.get_scan_checkpoint() is None

    def test_clear_checkpoint_when_none_exists_is_a_noop(self, staging) -> None:
        staging.clear_scan_checkpoint()
        assert staging.get_scan_checkpoint() is None

    def test_checkpoint_not_reset_by_read_only_activity(self, staging) -> None:
        """Regression: normal API/dashboard-style reads (status queries,
        listing scan runs, constructing a fresh instance and re-running
        init_db()) must never reset or touch an existing checkpoint."""
        from src.linkedin.staging import LinkedInStagingStorage

        staging.save_scan_checkpoint("scan-1", ["term-a\x1floc-a"], "term-b\x1floc-b", 2)

        for _ in range(20):
            staging.get_status()
            staging.list_scan_runs()
            staging.count_by_status()
            fresh = LinkedInStagingStorage(db_path=staging._db_path)
            fresh.init_db()
            fresh.get_scan_checkpoint()
            fresh.close()

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["current_query_key"] == "term-b\x1floc-b"
        assert cp["current_page"] == 2


# ---------------------------------------------------------------------------
# Pause/resume checkpoint state (issue #81)
# ---------------------------------------------------------------------------

class TestCheckpointLifecycleState:
    def test_save_defaults_to_running_and_stores_fingerprint(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 1, fingerprint="fp-1")
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "running"
        assert cp["config_fingerprint"] == "fp-1"

    def test_save_without_fingerprint_keeps_the_stored_one(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 1, fingerprint="fp-1")
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 2)
        assert staging.get_scan_checkpoint()["config_fingerprint"] == "fp-1"

    def test_set_state_keeps_progress(self, staging) -> None:
        staging.save_scan_checkpoint("scan-1", ["term-a\x1floc-a"], "term-b\x1floc-b", 4)
        staging.set_checkpoint_state("scan-1", "paused")
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert cp["current_query_key"] == "term-b\x1floc-b"
        assert cp["current_page"] == 4
        assert json.loads(cp["completed_queries_json"]) == ["term-a\x1floc-a"]

    def test_set_state_on_empty_table_creates_an_empty_progress_row(self, staging) -> None:
        staging.set_checkpoint_state("scan-1", "paused")
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert json.loads(cp["completed_queries_json"]) == []
        assert cp["current_query_key"] is None
        assert cp["current_page"] == 0

    def test_recover_marks_running_checkpoint_interrupted(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 3)
        staging.recover_stale_scans()
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "interrupted"
        assert cp["current_page"] == 3

    def test_recover_leaves_paused_checkpoint_and_run_alone(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.save_scan_checkpoint("scan-1", [], "term-a\x1floc-a", 3)
        staging.set_checkpoint_state("scan-1", "paused")
        staging.pause_scan_run("scan-1")
        assert staging.recover_stale_scans() == 0
        assert staging.get_scan_checkpoint()["state"] == "paused"
        assert staging.get_last_scan()["status"] == "paused"

    def test_legacy_checkpoint_table_is_migrated_without_losing_the_row(
        self, tmp_db: str
    ) -> None:
        import sqlite3

        from src.linkedin.staging import LinkedInStagingStorage

        raw = sqlite3.connect(tmp_db)
        raw.execute(
            """CREATE TABLE linkedin_scan_checkpoint (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   scan_id TEXT NOT NULL,
                   completed_queries_json TEXT NOT NULL DEFAULT '[]',
                   current_query_key TEXT,
                   current_page INTEGER NOT NULL DEFAULT 0,
                   updated_at TEXT NOT NULL)"""
        )
        raw.execute(
            "INSERT INTO linkedin_scan_checkpoint VALUES (1, 'old', '[]', 'k', 5, '2026-01-01')"
        )
        raw.commit()
        raw.close()

        LinkedInStagingStorage._initialized_paths.clear()
        store = LinkedInStagingStorage(db_path=tmp_db)
        store.init_db()
        cp = store.get_scan_checkpoint()
        assert cp["current_page"] == 5
        assert cp["state"] is None
        assert cp["config_fingerprint"] is None
        store.close()

    def test_checkpoint_column_migration_only_tolerates_duplicate_columns(
        self, tmp_db: str
    ) -> None:
        """Re-running the migration on an up-to-date database (or losing the
        concurrent-add race) is fine; any other ALTER error, such as a locked
        database, fails init_db (issue #81 re-review, R2)."""
        import sqlite3

        from src.linkedin.staging import LinkedInStagingStorage

        LinkedInStagingStorage._initialized_paths.clear()
        LinkedInStagingStorage(db_path=tmp_db).init_db()
        LinkedInStagingStorage._initialized_paths.clear()
        LinkedInStagingStorage(db_path=tmp_db).init_db()  # duplicate columns: no error

        class _LockedAlterConn:
            def __init__(self, real: sqlite3.Connection) -> None:
                self.real = real

            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE linkedin_scan_checkpoint"):
                    raise sqlite3.OperationalError("database is locked")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        LinkedInStagingStorage._initialized_paths.clear()
        store = LinkedInStagingStorage(db_path=tmp_db)
        real = store._get_conn()
        store._conn = _LockedAlterConn(real)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                store.init_db()
        finally:
            store._conn = real
        store.close()


def test_state_only_update_records_the_owning_scan(staging) -> None:
    staging.save_scan_checkpoint("old-scan", [], "term-a\x1floc-a", 2)
    staging.set_checkpoint_state("resumed-scan", "running", create_if_missing=False)
    cp = staging.get_scan_checkpoint()
    assert cp["scan_id"] == "resumed-scan"
    assert cp["current_page"] == 2
