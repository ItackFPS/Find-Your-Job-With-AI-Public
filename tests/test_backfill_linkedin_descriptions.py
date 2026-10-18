"""Tests for scripts/backfill_linkedin_descriptions.py.

Covers:
  - Records with missing descriptions are fetched and updated
  - Records with existing descriptions are skipped (idempotent)
  - dry_run mode reports without writing
  - Fetch returning None (login-gated) increments skipped count
  - Transferred jobs: jobs table + FTS updated
  - limit parameter restricts records processed
  - Running twice is safe (idempotent)
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(linkedin_job_id: str) -> str:
    return hashlib.sha256(f"linkedin:{linkedin_job_id}".encode()).hexdigest()[:32]


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """Return path to an initialized test database with both schemas."""
    db_path = str(tmp_path / "test.db")

    from src.linkedin.staging import LinkedInStagingStorage
    from src.storage.sqlite import Storage

    staging = LinkedInStagingStorage(db_path=db_path)
    staging.init_db()
    staging.close()

    storage = Storage(db_path=db_path)
    storage.init_db()
    storage.close()

    return db_path


def _insert_staging(
    db_path: str,
    linkedin_job_id: str,
    description: str | None = None,
    status: str = "waiting",
    work_mode: str | None = None,
) -> str:
    """Insert a minimal linkedin_staging row; return its stable_id."""
    stable_id = _stable_id(linkedin_job_id)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO linkedin_staging
           (id, linkedin_job_id, canonical_url, title, company, location,
            countries, is_remote, salary_currency, description,
            description_fetched, raw_payload, scan_run_id,
            discovered_at, last_seen_at, status, work_mode)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            stable_id,
            linkedin_job_id,
            f"https://www.linkedin.com/jobs/view/{linkedin_job_id}",
            "Test Job",
            "Acme",
            "Paris",
            "[]",
            0,
            "EUR",
            description,
            1 if description else 0,
            "{}",
            "scan-1",
            _now(),
            _now(),
            status,
            work_mode,
        ),
    )
    conn.commit()
    conn.close()
    return stable_id


def _insert_job(
    db_path: str,
    stable_id: str,
    linkedin_job_id: str,
    description: str | None = None,
) -> None:
    """Insert a minimal jobs table row."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO jobs
           (id, source, source_job_id, canonical_url, apply_url,
            company, title, description, location, countries,
            remote, salary_currency, discovered_at, skills,
            raw_payload, content_hash, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            stable_id,
            "linkedin",
            linkedin_job_id,
            f"https://www.linkedin.com/jobs/view/{linkedin_job_id}",
            f"https://www.linkedin.com/jobs/view/{linkedin_job_id}",
            "Acme",
            "Test Job",
            description,
            "Paris",
            "[]",
            0,
            "EUR",
            _now(),
            "[]",
            "{}",
            "",
            "active",
        ),
    )
    conn.commit()
    conn.close()


def _get_staging_desc(db_path: str, linkedin_job_id: str) -> tuple[str | None, int]:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT description, description_fetched FROM linkedin_staging "
        "WHERE linkedin_job_id = ?",
        (linkedin_job_id,),
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, 0)


def _get_jobs_desc(db_path: str, stable_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT description FROM jobs WHERE id = ?", (stable_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

LONG_DESC = "A" * 120  # > 50 chars threshold in fetch_linkedin_description
_CLASSIFIED_PATCH = "src.linkedin.scanner.fetch_linkedin_description_classified"


class TestBackfillFetches:
    def test_fetches_and_updates_missing_description(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "bf001")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        assert result["attempted"] == 1
        assert result["fetched"] == 1
        assert result["staging_updated"] == 1

        desc, fetched_flag = _get_staging_desc(tmp_db, "bf001")
        assert desc == LONG_DESC
        assert fetched_flag == 1

    def test_skips_records_with_existing_description(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "bf002", description="Already complete description here")

        with patch(_CLASSIFIED_PATCH) as mock_fetch:
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        mock_fetch.assert_not_called()
        assert result["total_eligible"] == 0
        assert result["staging_updated"] == 0

    def test_handles_fetch_returning_login_gated(self, tmp_db):
        """When fetch returns login_gated, record is not updated (stays retryable)."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "bf003")

        with patch(_CLASSIFIED_PATCH, return_value=(None, "login_gated")):
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        assert result["attempted"] == 1
        assert result["fetched"] == 0
        assert result["skipped_login_gate"] == 1
        assert result["staging_updated"] == 0

        desc, fetched_flag = _get_staging_desc(tmp_db, "bf003")
        assert desc is None
        assert fetched_flag == 0  # stays 0 so it can be retried

    def test_handles_fetch_returning_deleted(self, tmp_db):
        """When fetch returns 'deleted' (404), record is marked description_fetched=2."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "bf003d")

        with patch(_CLASSIFIED_PATCH, return_value=(None, "deleted")):
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        assert result["deleted"] == 1
        assert result["staging_updated"] == 0

        desc, fetched_flag = _get_staging_desc(tmp_db, "bf003d")
        assert desc is None
        assert fetched_flag == 2  # permanently unavailable

    def test_deleted_records_not_retried(self, tmp_db):
        """Records with description_fetched=2 are excluded from future backfill runs."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "bf003e")
        # Mark as permanently unavailable
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE linkedin_staging SET description_fetched=2 WHERE linkedin_job_id='bf003e'"
        )
        conn.commit()
        conn.close()

        with patch(_CLASSIFIED_PATCH) as mock_fetch:
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        mock_fetch.assert_not_called()
        assert result["total_eligible"] == 0

    def test_handles_fetch_raising_exception(self, tmp_db):
        """When fetch raises unexpectedly, the error is logged and backfill continues."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "bf004a")
        _insert_staging(tmp_db, "bf004b")

        call_count = [0]

        def flaky(job_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Network timeout")
            return LONG_DESC, "success"

        with patch(_CLASSIFIED_PATCH, side_effect=flaky):
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        assert result["attempted"] == 2
        assert result["failed"] == 1
        assert result["fetched"] == 1
        assert result["staging_updated"] == 1

    def test_multiple_records_all_processed(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        for i in range(1, 4):
            _insert_staging(tmp_db, f"bf005{i}")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(db_path=tmp_db, dry_run=False, delay=0)

        assert result["total_eligible"] == 3
        assert result["fetched"] == 3
        assert result["staging_updated"] == 3


class TestBackfillDryRun:
    def test_dry_run_does_not_write_to_db(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "dr001")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(db_path=tmp_db, dry_run=True, delay=0)

        assert result["staging_updated"] == 1  # counted but not written

        desc, fetched_flag = _get_staging_desc(tmp_db, "dr001")
        assert desc is None  # DB unchanged
        assert fetched_flag == 0

    def test_dry_run_counts_eligible_records(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        for i in range(3):
            _insert_staging(tmp_db, f"dr00{i+2}")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(db_path=tmp_db, dry_run=True, delay=0)

        assert result["total_eligible"] == 3
        assert result["staging_updated"] == 3


class TestBackfillIdempotency:
    def test_running_twice_is_safe(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "id001")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")) as mock_fetch:
            backfill(db_path=tmp_db, dry_run=False, delay=0)
            result2 = backfill(db_path=tmp_db, dry_run=False, delay=0)

        assert mock_fetch.call_count == 1  # only called once; second run finds nothing
        assert result2["total_eligible"] == 0
        assert result2["staging_updated"] == 0


class TestBackfillLimit:
    def test_limit_restricts_records_processed(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        for i in range(5):
            _insert_staging(tmp_db, f"lm00{i+1}")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(db_path=tmp_db, dry_run=False, delay=0, limit=2)

        assert result["attempted"] == 2
        assert result["staging_updated"] == 2


class TestBackfillStatusFilter:
    def test_status_filter_waiting_only(self, tmp_db):
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "sf001", status="waiting")
        _insert_staging(tmp_db, "sf002", status="transferred")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(
                db_path=tmp_db, dry_run=False, delay=0, status_filter="waiting"
            )

        assert result["total_eligible"] == 1
        assert result["staging_updated"] == 1
        desc_waiting, _ = _get_staging_desc(tmp_db, "sf001")
        desc_transferred, _ = _get_staging_desc(tmp_db, "sf002")
        assert desc_waiting == LONG_DESC
        assert desc_transferred is None  # not processed


class TestBackfillJobsTableUpdate:
    def test_updates_jobs_table_for_transferred_jobs(self, tmp_db):
        """When a transferred job lacks a description, both staging and jobs tables are updated."""
        from scripts.backfill_linkedin_descriptions import backfill

        stable_id = _insert_staging(tmp_db, "jt001", status="transferred")
        _insert_job(tmp_db, stable_id, "jt001", description=None)

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(
                db_path=tmp_db, dry_run=False, delay=0, status_filter="transferred"
            )

        assert result["jobs_table_updated"] == 1

        desc_staging, _ = _get_staging_desc(tmp_db, "jt001")
        assert desc_staging == LONG_DESC

        desc_jobs = _get_jobs_desc(tmp_db, stable_id)
        assert desc_jobs == LONG_DESC

    def test_does_not_overwrite_existing_jobs_description(self, tmp_db):
        """Jobs table is NOT updated when the job already has a description there."""
        from scripts.backfill_linkedin_descriptions import backfill

        stable_id = _insert_staging(tmp_db, "jt002", status="transferred")
        _insert_job(tmp_db, stable_id, "jt002", description="Existing description in jobs table")

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(
                db_path=tmp_db, dry_run=False, delay=0, status_filter="transferred"
            )

        assert result["jobs_table_updated"] == 0  # jobs table already had a description
        desc_jobs = _get_jobs_desc(tmp_db, stable_id)
        assert desc_jobs == "Existing description in jobs table"  # unchanged

    def test_waiting_records_do_not_update_jobs_table(self, tmp_db):
        """Jobs not yet transferred are not in the jobs table; no update attempted."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "jt003", status="waiting")
        # Do NOT insert a jobs table row

        with patch(_CLASSIFIED_PATCH, return_value=(LONG_DESC, "success")):
            result = backfill(
                db_path=tmp_db, dry_run=False, delay=0, status_filter="waiting"
            )

        assert result["jobs_table_updated"] == 0
        assert result["staging_updated"] == 1


class TestBackfillWorkModeDerivation:
    def test_work_mode_derived_from_description_if_missing(self, tmp_db):
        """When work_mode is NULL, it is derived from the fetched description."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "wm001", work_mode=None)

        remote_desc = "Fully remote position. " + "X" * 100

        with patch(_CLASSIFIED_PATCH, return_value=(remote_desc, "success")):
            backfill(db_path=tmp_db, dry_run=False, delay=0)

        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT work_mode, is_remote FROM linkedin_staging WHERE linkedin_job_id = 'wm001'"
        ).fetchone()
        conn.close()
        assert row[0] == "remote"
        assert row[1] == 1

    def test_existing_work_mode_not_overwritten(self, tmp_db):
        """When work_mode is already set, it is preserved regardless of description."""
        from scripts.backfill_linkedin_descriptions import backfill

        _insert_staging(tmp_db, "wm002", work_mode="on_site")

        remote_desc = "Fully remote position. " + "X" * 100

        with patch(_CLASSIFIED_PATCH, return_value=(remote_desc, "success")):
            backfill(db_path=tmp_db, dry_run=False, delay=0)

        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT work_mode FROM linkedin_staging WHERE linkedin_job_id = 'wm002'"
        ).fetchone()
        conn.close()
        assert row[0] == "on_site"  # preserved
