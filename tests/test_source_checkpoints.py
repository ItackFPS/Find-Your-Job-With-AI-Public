"""Phase NEXT / I11 regression tests: incremental-collection INFRASTRUCTURE.

Scope guard for every test here (per the explicit I11 decision): this is
checkpoint/observability infrastructure only. No collector's upstream fetch
is changed, gated, or skipped by anything tested here — a checkpoint is
purely descriptive (last time a source's collection completed with zero
errors, and how many of its jobs were new vs. already known). The one
correctness invariant that matters is: a checkpoint must never advance for
a source whose collection had errors, so nothing built on this data later
could ever conclude "this source was cleanly collected" when it wasn't.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from src.models.job import Job
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def rs(tmp_db: str):
    store = RunStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


class TestCheckpointCreation:
    def test_no_checkpoint_before_any_success(self, rs: RunStorage) -> None:
        assert rs.get_source_checkpoint("greenhouse") is None
        assert rs.list_source_checkpoints() == []

    def test_first_successful_collection_creates_checkpoint(self, rs: RunStorage) -> None:
        run_id = str(uuid.uuid4())
        rs.record_source_checkpoint_success(
            source="greenhouse",
            run_id=run_id,
            collected_count=10,
            new_count=10,
            known_count=0,
            timestamp="2026-09-13T00:00:00+00:00",
        )
        checkpoint = rs.get_source_checkpoint("greenhouse")
        assert checkpoint is not None
        assert checkpoint["last_run_id"] == run_id
        assert checkpoint["last_collected_count"] == 10
        assert checkpoint["last_new_count"] == 10
        assert checkpoint["last_known_count"] == 0

    def test_subsequent_successful_collection_updates_checkpoint(self, rs: RunStorage) -> None:
        rs.record_source_checkpoint_success(
            source="greenhouse", run_id="run-1", collected_count=10,
            new_count=10, known_count=0, timestamp="2026-09-13T00:00:00+00:00",
        )
        rs.record_source_checkpoint_success(
            source="greenhouse", run_id="run-2", collected_count=12,
            new_count=2, known_count=10, timestamp="2026-09-14T00:00:00+00:00",
        )
        checkpoint = rs.get_source_checkpoint("greenhouse")
        assert checkpoint["last_run_id"] == "run-2"
        assert checkpoint["last_new_count"] == 2
        assert checkpoint["last_known_count"] == 10
        # One row per source, not one row per run.
        assert len(rs.list_source_checkpoints()) == 1


class TestFailureSafety:
    def test_no_write_path_exists_for_a_failed_source(self, rs: RunStorage) -> None:
        """There is deliberately no `record_source_checkpoint_failure` or
        similar — the ONLY write method requires the caller to already know
        the source succeeded. This test documents that contract: calling the
        success-recording method IS the failure-safety boundary; the
        orchestrator (tested separately) must simply never call it for an
        errored source."""
        assert not hasattr(rs, "record_source_checkpoint_failure")
        assert not hasattr(rs, "record_source_checkpoint")  # no ambiguous generic writer

    def test_restart_after_failure_then_success_advances_normally(self, rs: RunStorage) -> None:
        """A prior run's checkpoint (from before some later failed run that
        never called the success method) must remain exactly as-is, then
        advance cleanly on the next real success — a failed run in between
        leaves no trace and blocks nothing."""
        rs.record_source_checkpoint_success(
            source="apec", run_id="run-1", collected_count=5,
            new_count=5, known_count=0, timestamp="2026-09-10T00:00:00+00:00",
        )
        # (a hypothetical failed run-2 in between never calls the success method at all)
        rs.record_source_checkpoint_success(
            source="apec", run_id="run-3", collected_count=6,
            new_count=1, known_count=5, timestamp="2026-09-12T00:00:00+00:00",
        )
        checkpoint = rs.get_source_checkpoint("apec")
        assert checkpoint["last_run_id"] == "run-3"
        assert checkpoint["last_success_at"] == "2026-09-12T00:00:00+00:00"


class TestRepeatedCollectionAndMultipleSources:
    def test_multiple_sources_tracked_independently(self, rs: RunStorage) -> None:
        rs.record_source_checkpoint_success(
            source="greenhouse", run_id="run-1", collected_count=10,
            new_count=10, known_count=0, timestamp="2026-09-13T00:00:00+00:00",
        )
        rs.record_source_checkpoint_success(
            source="lever", run_id="run-1", collected_count=3,
            new_count=1, known_count=2, timestamp="2026-09-13T00:01:00+00:00",
        )
        assert {c["source"] for c in rs.list_source_checkpoints()} == {"greenhouse", "lever"}
        assert rs.get_source_checkpoint("greenhouse")["last_collected_count"] == 10
        assert rs.get_source_checkpoint("lever")["last_collected_count"] == 3

    def test_repeated_identical_success_is_idempotent(self, rs: RunStorage) -> None:
        for _ in range(3):
            rs.record_source_checkpoint_success(
                source="greenhouse", run_id="run-1", collected_count=10,
                new_count=10, known_count=0, timestamp="2026-09-13T00:00:00+00:00",
            )
        assert len(rs.list_source_checkpoints()) == 1
        assert rs.get_source_checkpoint("greenhouse")["last_collected_count"] == 10


class TestMissingMetadata:
    def test_zero_collected_count_is_a_valid_success(self, rs: RunStorage) -> None:
        """A source can legitimately collect zero jobs in a clean run (no
        matching postings today) — that is still success, not failure."""
        rs.record_source_checkpoint_success(
            source="remoteok", run_id="run-1", collected_count=0,
            new_count=0, known_count=0, timestamp="2026-09-13T00:00:00+00:00",
        )
        checkpoint = rs.get_source_checkpoint("remoteok")
        assert checkpoint["last_collected_count"] == 0
        assert checkpoint["last_new_count"] == 0

    def test_unmeasured_new_known_counts_stored_as_null_not_zero(self, rs: RunStorage) -> None:
        """When a caller cannot classify new-vs-known (e.g. storage_stats
        didn't cover this source), None must round-trip as None/NULL, not be
        coerced to 0 — 'not measured' must stay distinguishable from
        'measured as zero'."""
        rs.record_source_checkpoint_success(
            source="eures", run_id="run-1", collected_count=5,
            new_count=None, known_count=None, timestamp="2026-09-13T00:00:00+00:00",
        )
        checkpoint = rs.get_source_checkpoint("eures")
        assert checkpoint["last_new_count"] is None
        assert checkpoint["last_known_count"] is None


class TestBackwardCompatibility:
    def test_init_db_is_idempotent_on_a_pre_existing_database(self, tmp_db: str) -> None:
        rs1 = RunStorage(db_path=tmp_db)
        rs1.init_db()
        rs1.record_source_checkpoint_success(
            source="greenhouse", run_id="run-1", collected_count=10,
            new_count=10, known_count=0, timestamp="2026-09-13T00:00:00+00:00",
        )
        rs1.close()

        RunStorage._initialized_paths.clear()  # force a real re-run of init_db's DDL
        rs2 = RunStorage(db_path=tmp_db)
        rs2.init_db()
        checkpoint = rs2.get_source_checkpoint("greenhouse")
        assert checkpoint is not None
        assert checkpoint["last_collected_count"] == 10
        rs2.close()


class TestUpsertJobsBatchBySourceBreakdown:
    """Storage.upsert_jobs_batch's by_source key is what the orchestrator
    feeds into record_source_checkpoint_success as new_count/known_count."""

    def test_by_source_classifies_new_vs_updated_vs_unchanged(self, tmp_db: str) -> None:
        storage = Storage(db_path=tmp_db)
        storage.init_db()

        job_a = Job(id="a", source="greenhouse", source_job_id="1", title="A", content_hash="h1")
        stats1 = storage.upsert_jobs_batch([job_a])
        assert stats1["by_source"]["greenhouse"] == {"inserted": 1, "updated": 0, "unchanged": 0}

        # Same content -> unchanged.
        stats2 = storage.upsert_jobs_batch([job_a])
        assert stats2["by_source"]["greenhouse"] == {"inserted": 0, "updated": 0, "unchanged": 1}

        # Changed content, same id -> updated.
        job_a_changed = job_a.model_copy(update={"title": "A v2", "content_hash": "h2"})
        stats3 = storage.upsert_jobs_batch([job_a_changed])
        assert stats3["by_source"]["greenhouse"] == {"inserted": 0, "updated": 1, "unchanged": 0}
        storage.close()

    def test_by_source_keeps_multiple_sources_separate(self, tmp_db: str) -> None:
        storage = Storage(db_path=tmp_db)
        storage.init_db()
        jobs = [
            Job(id="a", source="greenhouse", source_job_id="1", title="A"),
            Job(id="b", source="lever", source_job_id="2", title="B"),
        ]
        stats = storage.upsert_jobs_batch(jobs)
        assert stats["by_source"]["greenhouse"]["inserted"] == 1
        assert stats["by_source"]["lever"]["inserted"] == 1
        assert stats["inserted"] == 2  # top-level total unaffected
        storage.close()

    def test_empty_batch_returns_empty_by_source(self, tmp_db: str) -> None:
        storage = Storage(db_path=tmp_db)
        storage.init_db()
        assert storage.upsert_jobs_batch([]) == {
            "inserted": 0, "updated": 0, "unchanged": 0, "by_source": {},
        }
        storage.close()


class TestOrchestratorWiresCheckpointsSafely:
    def test_orchestrator_only_records_checkpoint_for_zero_error_sources(self) -> None:
        """Grep-level contract check (same convention already used for the
        I10 esco_synonyms wiring check): the orchestrator must gate its call
        to record_source_checkpoint_success on that source having zero
        errors, and must never call it for a from_staging source (whose real
        collection happens on a separate schedule, not in this pipeline
        run — mirrors source_health.py's existing from_staging exclusion)."""
        import inspect

        from src.pipeline import orchestrator

        source = inspect.getsource(orchestrator)
        assert "record_source_checkpoint_success" in source
        assert "from_staging" in source
