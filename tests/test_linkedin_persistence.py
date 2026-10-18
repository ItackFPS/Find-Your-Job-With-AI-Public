"""Regression tests for LinkedIn job persistence across pipeline runs.

Problem: previously accepted LinkedIn jobs disappeared from the API after a new
scan because the API scoped results to jobs scored in the *latest* run only, and
the pipeline only processed newly staged jobs (not previously stored ones).

Fix: the runner now re-injects all active LinkedIn jobs from the jobs table into
every pipeline run alongside the staging snapshot, so they are re-scored and
remain visible in the API.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job_row(
    job_id: str,
    source_job_id: str,
    title: str = "DevOps Engineer",
    company: str = "Acme",
    location: str = "Paris, France",
    status: str = "active",
    source: str = "linkedin",
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": job_id,
        "source": source,
        "source_job_id": source_job_id,
        "canonical_url": f"https://www.linkedin.com/jobs/view/{source_job_id}",
        "apply_url": None,
        "company": company,
        "title": title,
        "description": None,
        "location": location,
        "countries": json.dumps(["FR"]),
        "department": None,
        "remote": 0,
        "work_mode": "on_site",
        "employment_type": "full_time",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "EUR",
        "published_at": None,
        "updated_at": None,
        "discovered_at": now,
        "skills": json.dumps([]),
        "raw_payload": json.dumps({}),
        "content_hash": hashlib.md5((title + company).encode()).hexdigest(),
        "first_seen_at": now,
        "last_seen_at": now,
        "status": status,
    }


def _insert_job(conn, row: dict[str, Any]) -> None:
    cols = list(row.keys())
    ph = ", ".join("?" * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO jobs ({', '.join(cols)}) VALUES ({ph})",
        list(row.values()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Storage.get_active_jobs_by_source
# ---------------------------------------------------------------------------


class TestGetActiveJobsBySource:
    """Unit tests for the new Storage helper method."""

    @pytest.fixture
    def storage(self, tmp_path: Path):
        from src.storage.sqlite import Storage
        s = Storage(db_path=str(tmp_path / "test.db"))
        s.init_db()
        yield s
        s.close()

    @pytest.fixture
    def _conn(self, storage):
        return storage._get_conn()

    def test_returns_active_jobs(self, storage, _conn):
        row = _make_job_row("id-1", "li-111", status="active")
        _insert_job(_conn, row)
        jobs = storage.get_active_jobs_by_source("linkedin")
        assert len(jobs) == 1
        assert jobs[0].id == "id-1"

    def test_excludes_inactive_jobs(self, storage, _conn):
        _insert_job(_conn, _make_job_row("id-a", "li-aaa", status="active"))
        _insert_job(_conn, _make_job_row("id-b", "li-bbb", status="inactive"))
        jobs = storage.get_active_jobs_by_source("linkedin")
        assert len(jobs) == 1
        assert jobs[0].id == "id-a"

    def test_exclude_ids_param(self, storage, _conn):
        _insert_job(_conn, _make_job_row("id-1", "li-111", status="active"))
        _insert_job(_conn, _make_job_row("id-2", "li-222", status="active"))
        jobs = storage.get_active_jobs_by_source("linkedin", exclude_ids={"id-1"})
        assert len(jobs) == 1
        assert jobs[0].id == "id-2"

    def test_filters_by_source(self, storage, _conn):
        _insert_job(_conn, _make_job_row("id-li", "li-111", source="linkedin", status="active"))
        ft_row = _make_job_row("id-ft", "ft-999", source="france-travail", status="active")
        ft_row["source_job_id"] = "ft-999"
        _insert_job(_conn, ft_row)
        jobs = storage.get_active_jobs_by_source("linkedin")
        assert len(jobs) == 1
        assert jobs[0].source == "linkedin"

    def test_empty_when_no_jobs(self, storage):
        jobs = storage.get_active_jobs_by_source("linkedin")
        assert jobs == []

    def test_empty_exclude_ids_still_returns_all(self, storage, _conn):
        _insert_job(_conn, _make_job_row("id-1", "li-111", status="active"))
        jobs = storage.get_active_jobs_by_source("linkedin", exclude_ids=set())
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Runner: existing active jobs re-injected alongside staging snapshot
# ---------------------------------------------------------------------------


class TestRunnerLinkedInPersistence:
    """Integration test: runner injects existing active LinkedIn jobs.

    This is a unit-level test that mocks the heavy parts (pipeline execution,
    LinkedIn staging) and verifies that the runner appends previously stored
    active LinkedIn jobs to the prefetched batch.
    """

    def _make_runner(self, tmp_db: str):
        from src.api.runner import PipelineRunner
        return PipelineRunner(db_path=tmp_db, config_dir="config")

    @pytest.fixture
    def tmp_db(self, tmp_path: Path) -> str:
        from src.storage.sqlite import Storage
        s = Storage(db_path=str(tmp_path / "test.db"))
        s.init_db()
        s.close()
        return str(tmp_path / "test.db")

    def test_existing_active_jobs_included_when_no_staging(self, tmp_db):
        """When there are no new staging rows, existing active LinkedIn jobs still
        flow through the runner's prefetch list."""
        import sqlite3
        import threading

        # Insert an existing active LinkedIn job directly into jobs table.
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        row = _make_job_row("id-existing", "li-9999", title="Existing DevOps", status="active")
        _insert_job(conn, row)
        conn.close()

        from src.api.runner import PipelineRunner

        captured: list[list] = []
        run_called = threading.Event()

        def fake_run_pipeline(**kwargs):
            captured.append(kwargs.get("prefetched_linkedin_jobs", []))
            run_called.set()
            # Return a minimal failed result to avoid DB writes
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            from datetime import datetime, UTC
            return PipelineResult(
                run_id="fake",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                status="failed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
                error="test abort",
            )

        import src.api.runner as runner_mod
        original_run_pipeline = runner_mod.run_pipeline

        # Patch the run_pipeline import used in runner._execute
        runner_mod.run_pipeline = fake_run_pipeline

        try:
            runner = PipelineRunner(db_path=tmp_db, config_dir="config")
            # Directly invoke _execute (bypasses threading for test clarity)
            # We need a run_id in the DB first
            from src.storage.runs import RunStorage
            rs = RunStorage(db_path=tmp_db)
            rs.init_db()
            run_id = "test-run-001"
            rs.create_run(run_id)
            rs.close()

            # Run synchronously
            runner._execute(run_id)

            # Check that captured prefetched jobs contain the existing job
            assert len(captured) == 1, "run_pipeline should have been called once"
            prefetched = captured[0]
            ids = {j.id for j in prefetched}
            assert "id-existing" in ids, (
                "Existing active LinkedIn job must be re-injected into every pipeline run"
            )
        finally:
            runner_mod.run_pipeline = original_run_pipeline

    def test_snapshot_ids_excluded_from_existing_reload(self, tmp_db):
        """A job that was just staged must not be duplicated (it's already in
        prefetched_linkedin_jobs from the staging snapshot)."""
        import sqlite3

        # Insert an active LinkedIn job that is ALSO in the staging snapshot.
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        row = _make_job_row("id-new", "li-1111", title="New DevOps", status="active")
        _insert_job(conn, row)
        conn.close()

        from src.storage.sqlite import Storage
        s = Storage(db_path=tmp_db)
        s.init_db()
        # Exclude the same ID: should return empty list
        jobs = s.get_active_jobs_by_source("linkedin", exclude_ids={"id-new"})
        s.close()
        assert jobs == [], "Job already in staging snapshot must be excluded from re-inject"


# ---------------------------------------------------------------------------
# Helpers for staging-level tests
# ---------------------------------------------------------------------------


def _make_staging_record(
    linkedin_job_id: str,
    scan_run_id: str = "scan-001",
    description: str | None = None,
) -> dict[str, Any]:
    """Build a minimal linkedin_staging row dict."""
    import hashlib
    import json
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    stable_id = hashlib.sha256(f"linkedin:{linkedin_job_id}".encode()).hexdigest()[:32]
    return {
        "id": stable_id,
        "linkedin_job_id": linkedin_job_id,
        "canonical_url": f"https://www.linkedin.com/jobs/view/{linkedin_job_id}",
        "title": "DevOps Engineer",
        "company": "Acme Corp",
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


def _fake_pipeline_result(status: str = "failed"):
    from datetime import UTC, datetime
    from src.pipeline.orchestrator import PipelineResult, PipelineStats
    return PipelineResult(
        run_id="fake",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        status=status,
        stats=PipelineStats(),
        scored_jobs=[],
        rejection_records=[],
        error="test abort" if status == "failed" else None,
    )


def _run_with_capture(tmp_db: str, run_id: str = "test-run-001") -> list[Any]:
    """Invoke PipelineRunner._execute synchronously and capture prefetched_linkedin_jobs."""
    import src.api.runner as runner_mod
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage

    captured: list[list] = []

    def fake_run_pipeline(**kwargs):
        captured.append(list(kwargs.get("prefetched_linkedin_jobs") or []))
        return _fake_pipeline_result("failed")

    original = runner_mod.run_pipeline
    runner_mod.run_pipeline = fake_run_pipeline
    try:
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        rs.create_run(run_id)
        rs.close()
        runner = PipelineRunner(db_path=tmp_db, config_dir="config")
        runner._execute(run_id)
    finally:
        runner_mod.run_pipeline = original

    return captured[0] if captured else []


# ---------------------------------------------------------------------------
# Test A — First scan: all discovered jobs reach the pipeline
# ---------------------------------------------------------------------------


class TestScanA_FirstScan:
    """Test A: a first LinkedIn scan discovers N jobs; all N flow to the pipeline."""

    @pytest.fixture
    def tmp_db(self, tmp_path: Path) -> str:
        from src.storage.sqlite import Storage
        from src.linkedin.staging import LinkedInStagingStorage
        s = Storage(db_path=str(tmp_path / "test.db"))
        s.init_db()
        s.close()
        li = LinkedInStagingStorage(db_path=str(tmp_path / "test.db"))
        li.init_db()
        li.close()
        return str(tmp_path / "test.db")

    def test_first_scan_all_jobs_available_to_pipeline(self, tmp_db):
        """All 10 jobs from the first scan are passed to run_pipeline."""
        from src.linkedin.staging import LinkedInStagingStorage

        li = LinkedInStagingStorage(db_path=tmp_db)
        li.init_db()
        for i in range(10):
            li.insert_job(_make_staging_record(f"li-{i:04d}"))
        li.close()

        prefetched = _run_with_capture(tmp_db)

        assert len(prefetched) == 10, (
            f"Expected 10 LinkedIn jobs from first scan, got {len(prefetched)}"
        )
        # No duplicates
        ids = [j.id for j in prefetched]
        assert len(ids) == len(set(ids)), "Duplicate LinkedIn jobs in prefetched list"


# ---------------------------------------------------------------------------
# Test B — Second scan with overlap: existing + new = full corpus
# ---------------------------------------------------------------------------


class TestScanB_SecondScanWithOverlap:
    """Test B: second scan discovers 10 existing + 5 new → pipeline sees 15."""

    @pytest.fixture
    def tmp_db(self, tmp_path: Path) -> str:
        from src.storage.sqlite import Storage
        from src.linkedin.staging import LinkedInStagingStorage
        s = Storage(db_path=str(tmp_path / "test.db"))
        s.init_db()
        s.close()
        li = LinkedInStagingStorage(db_path=str(tmp_path / "test.db"))
        li.init_db()
        li.close()
        return str(tmp_path / "test.db")

    def test_existing_plus_new_equals_full_corpus(self, tmp_db):
        """Pipeline sees existing active jobs + new staging jobs = full LinkedIn corpus."""
        import sqlite3
        from src.linkedin.staging import LinkedInStagingStorage

        # Simulate 10 previously stored active LinkedIn jobs (from a prior scan+pipeline).
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        for i in range(10):
            row = _make_job_row(
                job_id=f"stored-id-{i:04d}",
                source_job_id=f"li-old-{i:04d}",
                title=f"Old Job {i}",
                status="active",
            )
            _insert_job(conn, row)
        conn.close()

        # Simulate 5 new jobs from the second scan (in staging as 'waiting').
        li = LinkedInStagingStorage(db_path=tmp_db)
        li.init_db()
        for i in range(5):
            li.insert_job(_make_staging_record(f"li-new-{i:04d}", scan_run_id="scan-002"))
        li.close()

        prefetched = _run_with_capture(tmp_db, run_id="test-run-002")

        assert len(prefetched) == 15, (
            f"Expected 15 LinkedIn jobs (10 existing + 5 new), got {len(prefetched)}"
        )
        ids = [j.id for j in prefetched]
        assert len(ids) == len(set(ids)), "Duplicate LinkedIn jobs detected"

    def test_new_scan_without_reinject_would_miss_existing(self, tmp_db):
        """Regression: without re-injection, only staged jobs (not corpus) would appear.

        This test documents the *wrong* behavior that the re-injection code prevents.
        It verifies that the pipeline sees MORE than just the staging snapshot.
        """
        import sqlite3
        from src.linkedin.staging import LinkedInStagingStorage

        # 8 previously stored active jobs (corpus built from prior runs).
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        for i in range(8):
            _insert_job(conn, _make_job_row(
                f"corpus-{i:04d}", f"li-corpus-{i:04d}", status="active"
            ))
        conn.close()

        # 3 new staging jobs from current scan.
        li = LinkedInStagingStorage(db_path=tmp_db)
        li.init_db()
        for i in range(3):
            li.insert_job(_make_staging_record(f"li-fresh-{i:04d}"))
        li.close()

        prefetched = _run_with_capture(tmp_db, run_id="test-run-003")

        # With re-injection: 8 (corpus) + 3 (new) = 11.
        # Without re-injection: would be only 3.
        assert len(prefetched) == 11, (
            f"Re-injection must include corpus jobs; got {len(prefetched)}, "
            f"expected 11 (8 existing + 3 new). "
            f"If this fails, re-injection is broken."
        )


# ---------------------------------------------------------------------------
# Test C — Repeated scan: no duplication
# ---------------------------------------------------------------------------


class TestScanC_RepeatedScan:
    """Test C: running pipeline again with same jobs produces no duplicates."""

    @pytest.fixture
    def tmp_db(self, tmp_path: Path) -> str:
        from src.storage.sqlite import Storage
        from src.linkedin.staging import LinkedInStagingStorage
        s = Storage(db_path=str(tmp_path / "test.db"))
        s.init_db()
        s.close()
        li = LinkedInStagingStorage(db_path=str(tmp_path / "test.db"))
        li.init_db()
        li.close()
        return str(tmp_path / "test.db")

    def test_repeated_run_no_new_staging_same_corpus(self, tmp_db):
        """When no new staging jobs exist, the active corpus is re-injected without duplication."""
        import sqlite3

        # 12 active LinkedIn jobs in the corpus (no new staging).
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        for i in range(12):
            _insert_job(conn, _make_job_row(
                f"corp-{i:04d}", f"li-corp-{i:04d}", status="active"
            ))
        conn.close()

        prefetched = _run_with_capture(tmp_db, run_id="test-run-repeat")

        assert len(prefetched) == 12, (
            f"Repeated run must return same 12-job corpus, got {len(prefetched)}"
        )
        ids = [j.id for j in prefetched]
        assert len(ids) == len(set(ids)), "Duplicate LinkedIn jobs in repeated run"

    def test_staging_snapshot_job_not_duplicated_after_pipeline(self, tmp_db):
        """A job from staging that lands in the jobs table must not appear twice
        in a subsequent run (once from staging snapshot + once from re-injection)."""
        import sqlite3
        from src.linkedin.staging import LinkedInStagingStorage

        # 5 existing active jobs.
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        for i in range(5):
            _insert_job(conn, _make_job_row(
                f"exist-{i:04d}", f"li-exist-{i:04d}", status="active"
            ))
        conn.close()

        # 3 new jobs in staging.
        li = LinkedInStagingStorage(db_path=tmp_db)
        li.init_db()
        for i in range(3):
            li.insert_job(_make_staging_record(f"li-staged-{i:04d}"))
        li.close()

        # First run: sees 5 existing + 3 staged = 8.
        prefetched_run1 = _run_with_capture(tmp_db, run_id="test-run-c1")
        assert len(prefetched_run1) == 8

        # Simulate those 3 staged jobs landing in the jobs table as active.
        # (In production the pipeline does this; here we do it manually.)
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        for i in range(3):
            import hashlib
            jid = f"li-staged-{i:04d}"
            stable_id = hashlib.sha256(f"linkedin:{jid}".encode()).hexdigest()[:32]
            _insert_job(conn, _make_job_row(stable_id, jid, title=f"Staged {i}", status="active"))
        conn.close()

        # Second run: no new staging → re-injection gives 5 + 3 = 8, no duplicates.
        prefetched_run2 = _run_with_capture(tmp_db, run_id="test-run-c2")
        assert len(prefetched_run2) == 8, (
            f"Second run must see same 8 unique jobs, got {len(prefetched_run2)}"
        )
        ids = [j.id for j in prefetched_run2]
        assert len(ids) == len(set(ids)), "Duplicate LinkedIn jobs after corpus expansion"


# ---------------------------------------------------------------------------
# Test D — Description present: stored in staging and transferred to Job
# ---------------------------------------------------------------------------


class TestDescriptionD_DescriptionPresent:
    """Test D: a LinkedIn result with a description stores it end-to-end."""

    def test_description_persisted_in_staging(self, tmp_path: Path):
        """When a staging row has a description, it is stored and retrievable."""
        from src.linkedin.staging import LinkedInStagingStorage

        db_path = str(tmp_path / "test.db")
        li = LinkedInStagingStorage(db_path=db_path)
        li.init_db()
        rec = _make_staging_record("li-desc-001", description="We are looking for a DevOps engineer.")
        li.insert_job(rec)

        rows = li.snapshot_waiting("run-d-001")
        li.close()

        assert len(rows) == 1
        assert rows[0]["description"] == "We are looking for a DevOps engineer."
        assert rows[0]["description_fetched"] == 1

    def test_description_transferred_to_job_object(self, tmp_path: Path):
        """staging_row_to_job preserves the description in the resulting Job."""
        from src.linkedin.staging import staging_row_to_job

        row = _make_staging_record("li-desc-002", description="Exciting cloud engineering role.")
        job = staging_row_to_job(row)

        assert job.description == "Exciting cloud engineering role."
        assert job.source == "linkedin"
        assert job.id is not None

    def test_description_survives_upsert_to_jobs_table(self, tmp_path: Path):
        """A LinkedIn job with a description is stored with that description in jobs."""
        from src.linkedin.staging import staging_row_to_job
        from src.storage.sqlite import Storage

        db_path = str(tmp_path / "test.db")
        s = Storage(db_path=db_path)
        s.init_db()

        row = _make_staging_record("li-desc-003", description="Python backend role in Paris.")
        job = staging_row_to_job(row)
        s.upsert_job(job)

        retrieved = s.get_active_jobs_by_source("linkedin")
        s.close()

        assert len(retrieved) == 1
        assert retrieved[0].description == "Python backend role in Paris."


# ---------------------------------------------------------------------------
# Test E — Missing description: job preserved without crash
# ---------------------------------------------------------------------------


class TestDescriptionE_MissingDescription:
    """Test E: a LinkedIn result without a description is preserved gracefully."""

    def test_no_description_staging_row_stored(self, tmp_path: Path):
        """Staging row with no description is inserted without error."""
        from src.linkedin.staging import LinkedInStagingStorage

        db_path = str(tmp_path / "test.db")
        li = LinkedInStagingStorage(db_path=db_path)
        li.init_db()
        rec = _make_staging_record("li-nodesc-001", description=None)
        is_new = li.insert_job(rec)
        li.close()

        assert is_new is True, "No-description job must be inserted as new"

    def test_no_description_job_object_has_none_description(self, tmp_path: Path):
        """staging_row_to_job returns a valid Job with description=None."""
        from src.linkedin.staging import staging_row_to_job

        row = _make_staging_record("li-nodesc-002", description=None)
        job = staging_row_to_job(row)

        assert job.description is None
        assert job.title == "DevOps Engineer"
        assert job.id is not None

    def test_no_description_job_stored_in_jobs_table(self, tmp_path: Path):
        """A no-description LinkedIn job is stored in the jobs table without crashing."""
        from src.linkedin.staging import staging_row_to_job
        from src.storage.sqlite import Storage

        db_path = str(tmp_path / "test.db")
        s = Storage(db_path=db_path)
        s.init_db()

        row = _make_staging_record("li-nodesc-003", description=None)
        job = staging_row_to_job(row)
        s.upsert_job(job)

        retrieved = s.get_active_jobs_by_source("linkedin")
        s.close()

        assert len(retrieved) == 1
        assert retrieved[0].description is None

    def test_empty_description_treated_as_absent(self, tmp_path: Path):
        """An empty-string description is normalized to None in the Job object."""
        from src.linkedin.staging import staging_row_to_job

        row = _make_staging_record("li-nodesc-004", description="")
        job = staging_row_to_job(row)

        assert job.description is None, "Empty description must be normalized to None"

    def test_no_description_does_not_block_pipeline_reinjection(self, tmp_path: Path):
        """A no-description job in the jobs table is re-injected without error."""
        import sqlite3
        from src.storage.sqlite import Storage
        from src.linkedin.staging import LinkedInStagingStorage

        db_path = str(tmp_path / "test.db")
        s = Storage(db_path=db_path)
        s.init_db()
        s.close()
        li = LinkedInStagingStorage(db_path=db_path)
        li.init_db()
        li.close()

        # Insert a no-description active job.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = _make_job_row("id-nodesc", "li-nodesc-005", status="active")
        row["description"] = None
        _insert_job(conn, row)
        conn.close()

        prefetched = _run_with_capture(db_path, run_id="test-run-nodesc")
        assert len(prefetched) == 1
        assert prefetched[0].description is None
