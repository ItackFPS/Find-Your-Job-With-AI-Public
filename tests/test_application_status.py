"""Tests for job application status: persistence and pipeline isolation.

Verifies that:
- application_status defaults to None for new jobs
- upsert_job does NOT overwrite an existing application_status
- upsert_jobs_batch does NOT overwrite an existing application_status
- update_application_status writes the value correctly
- All valid status values round-trip cleanly
- Clearing (None) works
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.storage.sqlite import Storage


def _make_job(**kwargs) -> Job:
    base = {
        "source": "test_source",
        "source_job_id": "JOB-001",
        "title": "Software Engineer",
        "company": "Acme Corp",
        "location": "Paris, France",
        "content_hash": "abc123",
    }
    base.update(kwargs)
    return Job(**base)


@pytest.fixture()
def storage(tmp_path):
    db = str(tmp_path / "test.db")
    s = Storage(db_path=db)
    s.init_db()
    yield s
    s.close()


class TestApplicationStatusDefault:
    def test_new_job_has_null_application_status(self, storage):
        job = _make_job()
        storage.upsert_job(job)
        retrieved = storage.get_job_by_id(job.id)
        assert retrieved is not None
        assert retrieved.application_status is None

    def test_new_jobs_batch_have_null_application_status(self, storage):
        # Use distinct content_hashes to avoid intra-batch content dedup
        jobs = [_make_job(source_job_id=f"J{i}", content_hash=f"hash{i}") for i in range(3)]
        storage.upsert_jobs_batch(jobs)
        for job in jobs:
            retrieved = storage.get_job_by_id(job.id)
            assert retrieved is not None
            assert retrieved.application_status is None


class TestUpdateApplicationStatus:
    @pytest.mark.parametrize("status", [
        "to_apply", "applied", "waiting", "interview",
        "accepted", "rejected", "denied",
    ])
    def test_all_valid_statuses_persist(self, storage, status):
        job = _make_job()
        storage.upsert_job(job)
        storage.update_application_status(job.id, status)
        retrieved = storage.get_job_by_id(job.id)
        assert retrieved is not None
        assert retrieved.application_status == status

    def test_clear_status(self, storage):
        job = _make_job()
        storage.upsert_job(job)
        storage.update_application_status(job.id, "applied")
        storage.update_application_status(job.id, None)
        retrieved = storage.get_job_by_id(job.id)
        assert retrieved is not None
        assert retrieved.application_status is None


class TestPipelinePreservesApplicationStatus:
    def test_upsert_job_preserves_status(self, storage):
        """Calling upsert_job again (as the pipeline does) must not wipe the user's status."""
        job = _make_job()
        storage.upsert_job(job)
        storage.update_application_status(job.id, "applied")

        # Simulate a pipeline re-run that upserts the same job
        storage.upsert_job(job)

        retrieved = storage.get_job_by_id(job.id)
        assert retrieved is not None
        assert retrieved.application_status == "applied", (
            "upsert_job must preserve application_status"
        )

    def test_upsert_jobs_batch_preserves_status(self, storage):
        """upsert_jobs_batch must not overwrite user-set application_status."""
        job = _make_job()
        storage.upsert_jobs_batch([job])
        storage.update_application_status(job.id, "interview")

        # Simulate pipeline re-run with updated content
        updated_job = _make_job(title="Senior Software Engineer", content_hash="def456")
        storage.upsert_jobs_batch([updated_job])

        retrieved = storage.get_job_by_id(job.id)
        assert retrieved is not None
        assert retrieved.application_status == "interview", (
            "upsert_jobs_batch must preserve application_status on content update"
        )

    def test_upsert_batch_unchanged_preserves_status(self, storage):
        """upsert_jobs_batch with unchanged content must not overwrite application_status."""
        job = _make_job()
        storage.upsert_jobs_batch([job])
        storage.update_application_status(job.id, "accepted")

        # Re-upsert same job (unchanged content_hash)
        storage.upsert_jobs_batch([job])

        retrieved = storage.get_job_by_id(job.id)
        assert retrieved is not None
        assert retrieved.application_status == "accepted"

    def test_new_jobs_in_batch_get_null_status(self, storage):
        """New jobs inserted via batch must start with null application_status."""
        existing = _make_job(source_job_id="EXIST-1", content_hash="hash-exist")
        storage.upsert_jobs_batch([existing])
        storage.update_application_status(existing.id, "waiting")

        # New job must have a distinct content_hash to avoid cross-run dedup
        new_job = _make_job(source_job_id="NEW-1", content_hash="hash-new")
        storage.upsert_jobs_batch([existing, new_job])

        # Existing job's status must be preserved
        existing_retrieved = storage.get_job_by_id(existing.id)
        assert existing_retrieved.application_status == "waiting"

        # New job must start with None
        new_retrieved = storage.get_job_by_id(new_job.id)
        assert new_retrieved is not None
        assert new_retrieved.application_status is None
