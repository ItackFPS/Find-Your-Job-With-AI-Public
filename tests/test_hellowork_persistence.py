"""Regression tests for HelloWork job persistence across pipeline runs.

Mirrors tests/test_linkedin_persistence.py: the runner must (1) snapshot
waiting HelloWork staging rows into the pipeline run, (2) re-inject
previously stored active HelloWork jobs so they don't disappear from the
API between scans, and (3) finalize the snapshot (transferred/processed on
success, reset to waiting on failure) without duplicating jobs across runs.
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
    title: str = "Développeur Python",
    company: str = "Acme",
    location: str = "Paris, France",
    status: str = "active",
    source: str = "hellowork",
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": job_id,
        "source": source,
        "source_job_id": source_job_id,
        "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{source_job_id}.html",
        "apply_url": None,
        "company": company,
        "title": title,
        "description": None,
        "location": location,
        "countries": json.dumps(["FR"]),
        "department": None,
        "remote": 0,
        "work_mode": "on_site",
        "employment_type": "full-time",
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
    conn.execute(f"INSERT OR REPLACE INTO jobs ({', '.join(cols)}) VALUES ({ph})", list(row.values()))
    conn.commit()


def _make_staging_record(source_job_id: str, scan_run_id: str = "scan-001") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    stable_id = hashlib.sha256(f"hellowork:{source_job_id}".encode()).hexdigest()[:32]
    return {
        "id": stable_id,
        "source_job_id": source_job_id,
        "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{source_job_id}.html",
        "title": "Développeur Python",
        "company": "Acme",
        "location": "Paris, France",
        "countries": json.dumps(["FR"]),
        "is_remote": 0,
        "work_mode": "on_site",
        "employment_type": "full-time",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "EUR",
        "description": "A great job",
        "published_at": None,
        "raw_payload": "{}",
        "scan_run_id": scan_run_id,
        "discovered_at": now,
        "last_seen_at": now,
    }


def _fake_pipeline_result(status: str = "failed", scored_jobs: list[Any] | None = None):
    from src.pipeline.orchestrator import PipelineResult, PipelineStats
    return PipelineResult(
        run_id="fake",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        status=status,
        stats=PipelineStats(),
        scored_jobs=scored_jobs or [],
        rejection_records=[],
        error="test abort" if status == "failed" else None,
    )


def _run_with_capture(
    tmp_db: str, run_id: str = "test-run-001", result_factory=lambda: _fake_pipeline_result("failed")
) -> list[Any]:
    """Invoke PipelineRunner._execute synchronously and capture prefetched_hellowork_jobs."""
    import src.api.runner as runner_mod
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage

    captured: list[list] = []

    def fake_run_pipeline(**kwargs):
        captured.append(list(kwargs.get("prefetched_hellowork_jobs") or []))
        return result_factory()

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


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    from src.storage.sqlite import Storage
    s = Storage(db_path=str(tmp_path / "test.db"))
    s.init_db()
    s.close()
    return str(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Existing-active-job re-injection
# ---------------------------------------------------------------------------

class TestRunnerHelloWorkPersistence:
    def test_existing_active_jobs_included_when_no_staging(self, tmp_db) -> None:
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        _insert_job(conn, _make_job_row("id-existing", "83000001", title="Existing Dev"))
        conn.close()

        prefetched = _run_with_capture(tmp_db)
        ids = {j.id for j in prefetched}
        assert "id-existing" in ids, "Existing active HelloWork job must be re-injected into every pipeline run"

    def test_snapshot_ids_excluded_from_existing_reload(self, tmp_db) -> None:
        import sqlite3
        from src.storage.sqlite import Storage

        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        _insert_job(conn, _make_job_row("id-new", "83000002", title="New Dev"))
        conn.close()

        s = Storage(db_path=tmp_db)
        s.init_db()
        jobs = s.get_active_jobs_by_source("hellowork", exclude_ids={"id-new"})
        s.close()
        assert jobs == []


# ---------------------------------------------------------------------------
# Scan A — first scan: all discovered jobs reach the pipeline
# ---------------------------------------------------------------------------

class TestScanA_FirstScan:
    def test_first_scan_all_jobs_available_to_pipeline(self, tmp_db) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        staging.insert_job(_make_staging_record("83000010"))
        staging.insert_job(_make_staging_record("83000011"))
        staging.close()

        prefetched = _run_with_capture(tmp_db)
        source_ids = {j.source_job_id for j in prefetched}
        assert source_ids == {"83000010", "83000011"}


# ---------------------------------------------------------------------------
# Scan B — second scan with overlap: existing + new = full corpus
# ---------------------------------------------------------------------------

class TestScanB_SecondScanWithOverlap:
    def test_existing_plus_new_equals_full_corpus(self, tmp_db) -> None:
        import sqlite3
        from src.hellowork.staging import HelloWorkStagingStorage

        # Job A already stored from a previous successful run.
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        _insert_job(conn, _make_job_row("id-A", "83000020", title="Job A"))
        conn.close()

        # Job B newly staged, waiting for this run.
        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        staging.insert_job(_make_staging_record("83000021"))
        staging.close()

        prefetched = _run_with_capture(tmp_db)
        source_ids = {getattr(j, "source_job_id", None) for j in prefetched}
        assert "83000020" in source_ids
        assert "83000021" in source_ids

    def test_new_scan_without_reinject_would_miss_existing(self, tmp_db) -> None:
        """Sanity check for the bug this mechanism fixes: a snapshot alone
        (without re-injecting existing active jobs) would not contain job A."""
        from src.hellowork.staging import HelloWorkStagingStorage

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        staging.insert_job(_make_staging_record("83000030"))
        snapshot = staging.snapshot_waiting("run-x")
        staging.close()
        source_ids = {r["source_job_id"] for r in snapshot}
        assert source_ids == {"83000030"}  # only the newly staged job, no history


# ---------------------------------------------------------------------------
# Scan C — repeated scan: no duplication
# ---------------------------------------------------------------------------

class TestScanC_RepeatedScan:
    def test_repeated_run_no_new_staging_same_corpus(self, tmp_db) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        staging.insert_job(_make_staging_record("83000040"))
        staging.close()

        first = _run_with_capture(tmp_db, run_id="run-1")
        second = _run_with_capture(tmp_db, run_id="run-2")
        # First run snapshots the waiting row; a failed run resets it to
        # waiting, so the second run should see the same single job again
        # (no duplicate rows created).
        assert {j.source_job_id for j in first} == {"83000040"}
        assert {j.source_job_id for j in second} == {"83000040"}

    def test_staging_snapshot_job_not_duplicated_after_pipeline(self, tmp_db) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        staging.insert_job(_make_staging_record("83000050"))
        staging.close()

        _run_with_capture(tmp_db, run_id="run-1")

        staging2 = HelloWorkStagingStorage(db_path=tmp_db)
        count = staging2._get_conn().execute("SELECT COUNT(*) FROM hellowork_staging").fetchone()[0]
        staging2.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Snapshot finalisation: transferred / processed / reset
# ---------------------------------------------------------------------------

class TestSnapshotFinalisation:
    def test_successful_run_marks_transferred_and_processed(self, tmp_db) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage
        from src.pipeline.orchestrator import ScoredJobRecord

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        r1 = _make_staging_record("83000060")
        r2 = _make_staging_record("83000061")
        staging.insert_job(r1)
        staging.insert_job(r2)
        staging.close()

        def make_success_result():
            scored = [ScoredJobRecord(
                job_id=r1["id"], job_title="x", job_company="y", job_source="hellowork",
                final_score=0.9, deterministic_score=0.9, semantic_score=0.9,
                factors={}, personalization_adjustment=0.0,
            )]
            return _fake_pipeline_result("completed", scored_jobs=scored)

        _run_with_capture(tmp_db, run_id="run-1", result_factory=make_success_result)

        staging2 = HelloWorkStagingStorage(db_path=tmp_db)
        counts = staging2.count_by_status()
        staging2.close()
        assert counts.get("transferred") == 1
        assert counts.get("processed") == 1

    def test_failed_run_resets_to_waiting(self, tmp_db) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()
        staging.insert_job(_make_staging_record("83000070"))
        staging.close()

        _run_with_capture(tmp_db, run_id="run-1", result_factory=lambda: _fake_pipeline_result("failed"))

        staging2 = HelloWorkStagingStorage(db_path=tmp_db)
        counts = staging2.count_by_status()
        staging2.close()
        assert counts.get("waiting") == 1
