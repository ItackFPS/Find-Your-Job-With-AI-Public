"""Integration test that exercises the real Phase 1 pipeline:
COLLECT → NORMALIZE → VALIDATE → DEDUPLICATE → FILTER → SCORE → STORE

This test uses in-memory fixtures to avoid external API dependencies.
It validates the full pipeline flow as implemented in src/main.py.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from src.models.source import Source
from src.storage.sqlite import Storage


class TestFullPipelineIntegration:
    """End-to-end integration test of the Phase 1 pipeline."""

    @pytest.fixture
    def temp_db(self):
        """Create a unique temporary database file for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        yield str(db_path)
        try:
            db_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    @pytest.fixture
    def sample_jobs(self):
        """Create a set of sample jobs for testing the pipeline."""
        now = datetime.now(UTC)

        return [
            Job(
                id="j1", source="greenhouse", title="Software Engineer",
                company="Acme Corp", description="Backend developer role",
                location="Paris, France", countries=["France"], remote=True,
                employment_type="full-time", salary_min=80000, salary_max=120000,
                salary_currency="EUR", published_at=now - timedelta(days=3),
            ),
            Job(
                id="j2", source="ashby", title="Frontend Developer",
                company="TechStart Inc", description="UI/UX developer role",
                location="London, UK", countries=["UK"], remote=True,
                employment_type="full-time", salary_min=70000, salary_max=110000,
                salary_currency="EUR", published_at=now - timedelta(days=5),
            ),
            Job(
                id="j3", source="linkedin", title="Junior Software Engineer",
                company="StartupXYZ", description="Entry level developer role",
                location="Berlin, Germany", countries=["Germany"], remote=False,
                employment_type="full-time", salary_min=50000, salary_max=70000,
                salary_currency="EUR", published_at=now - timedelta(days=1),
            ),
            Job(
                id="j4", source="greenhouse", title="Software Engineer",
                company="Acme Corp", description="Backend developer role",
                location="Paris, France", countries=["France"], remote=True,
                employment_type="full-time", salary_min=80000, salary_max=120000,
                salary_currency="EUR", published_at=now - timedelta(days=3),
            ),  # Duplicate of j1 (same source + title)
            Job(
                id="j5", source="remote", title="Contract Developer",
                company="FreelanceHub", description="Freelance contract only",
                location="", countries=[], remote=True,
                employment_type="contract", salary_min=60000, salary_max=None,
                salary_currency="EUR", published_at=now - timedelta(days=14),
            ),  # Will be rejected by filter (posted > 7 days) and excluded keyword
        ]

    @pytest.fixture
    def sample_sources(self):
        """Create sample source configurations."""
        return [
            Source(name="greenhouse-public", type="greenhouse", active=True),
            Source(name="ashby-public", type="ashby", active=True),
            Source(name="weworkremotely-rss", type="rss", active=True),
        ]

    def test_full_pipeline(self, temp_db, sample_jobs):
        """Run the complete pipeline: COLLECT → NORMALIZE → VALIDATE → DEDUPLICATE → FILTER → SCORE → STORE."""
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filters
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.validate import validate_jobs

        # 1. COLLECT (simulated)
        all_jobs = sample_jobs
        print(f"\n[Phase 1] COLLECT: {len(all_jobs)} jobs collected")

        # 2. NORMALIZE
        normalized = normalize_jobs("all", all_jobs)
        print(f"[Phase 1] NORMALIZE: {len(normalized)} jobs normalized")

        # 3. VALIDATE
        valid, errors = validate_jobs(normalized)
        print(f"[Phase 1] VALIDATE: {len(valid)} valid, {len(errors)} errors")

        # 4. DEDUPLICATE
        deduped, duplicates_removed = deduplicate_jobs(valid)
        print(f"[Phase 1] DEDUPLICATE: removed {duplicates_removed} duplicates, {len(deduped)} remaining")

        # 5. FILTER
        config_path = "config/filters.yaml"
        if Path(config_path).exists():
            filters_config = load_filters(config_path)
            filtered, rejected = filter_jobs(deduped, filters_config)
            print(f"[Phase 1] FILTER: {len(filtered)} passed, {rejected} rejected")
        else:
            print("[Phase 1] FILTER: config not found, skipping")
            filtered = deduped

        # 6. SCORE
        scoring_path = "config/scoring.yaml"
        if Path(scoring_path).exists():
            scoring_config = load_scoring_config(scoring_path)
            scored = score_jobs(filtered, scoring_config)
            print(f"[Phase 1] SCORE: {len(scored)} jobs scored")

            # Sort by score descending
            scored.sort(key=lambda x: x[1], reverse=True)
        else:
            print("[Phase 1] SCORE: config not found, skipping")
            scored = [(j, 0.5) for j in filtered]

        # 7. STORE
        storage = Storage(db_path=str(Path(temp_db)))
        storage.init_db()
        stored_count = 0
        for job, _ in scored:
            storage.upsert_job(job)
            stored_count += 1
        storage.close()

        # Verify storage
        total_stored = storage.count_jobs()
        assert total_stored == stored_count, f"Expected {stored_count} jobs in DB, got {total_stored}"

        # Verify all scored jobs are retrievable
        retrieved = storage.get_jobs(limit=len(scored))
        assert len(retrieved) >= len(scored), f"Expected at least {len(scored)} jobs, got {len(retrieved)}"

        print(f"[Phase 1] STORE: {stored_count} jobs stored")
        print(f"\n[Phase 1] COMPLETE: {stored_count} jobs stored in database")

    def test_pipeline_with_empty_sources(self, temp_db):
        """Pipeline should handle empty job lists gracefully."""
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.validate import validate_jobs

        jobs = []  # No jobs collected

        normalized = normalize_jobs("all", jobs)
        valid, _errors = validate_jobs(normalized)
        _deduped, _ = deduplicate_jobs(valid)

        storage = Storage(db_path=str(Path(temp_db)))
        storage.init_db()
        storage.close()

        assert storage.count_jobs() == 0

    def test_pipeline_with_invalid_jobs(self, temp_db):
        """Pipeline should handle jobs that fail validation gracefully."""
        from src.models.job import Job
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.validate import validate_jobs

        # Create a job with missing required fields
        invalid_job = Job(
            id="j1", source="greenhouse", title="", company="",
            description="", location="", countries=[], remote=False,
            employment_type=None, salary_min=None, salary_max=None,
        )

        normalized = normalize_jobs("all", [invalid_job])
        valid, _errors = validate_jobs(normalized)
        deduped, _ = deduplicate_jobs(valid)

        storage = Storage(db_path=str(Path(temp_db)))
        storage.init_db()
        for job in deduped:
            storage.upsert_job(job)
        storage.close()

        # Invalid jobs should not be stored (they were filtered out by validation)
        assert storage.count_jobs() == 0

    def test_pipeline_with_duplicate_sources(self, temp_db):
        """Pipeline should handle duplicate jobs from same source correctly."""
        from src.models.job import Job
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.validate import validate_jobs

        jobs = [
            Job(id="j1", source="greenhouse", title="Software Engineer", company="Acme Corp",
                description="Backend role", location="Paris, France", countries=["France"], remote=True),
            Job(id="j2", source="greenhouse", title="Software Engineer", company="Acme Corp",
                description="Backend role", location="Paris, France", countries=["France"], remote=True),  # Exact duplicate
        ]

        normalized = normalize_jobs("all", jobs)
        valid, _errors = validate_jobs(normalized)
        deduped, duplicates_removed = deduplicate_jobs(valid)

        storage = Storage(db_path=str(Path(temp_db)))
        storage.init_db()
        for job in deduped:
            storage.upsert_job(job)
        storage.close()

        assert storage.count_jobs() == 1
        assert duplicates_removed > 0

    def test_pipeline_full_cycle(self, temp_db):
        """Full pipeline cycle with all phases."""
        from src.models.job import Job
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filters
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.validate import validate_jobs

        now = datetime.now(UTC)
        jobs = [
            Job(id="j1", source="greenhouse", title="Software Engineer", company="Acme Corp",
                description="Backend developer role", location="Paris, France", countries=["France"], remote=True,
                employment_type="full-time", salary_min=80000, salary_max=120000,
                salary_currency="EUR", published_at=now - timedelta(days=3)),
            Job(id="j2", source="ashby", title="Frontend Developer", company="TechStart Inc",
                description="UI/UX developer role", location="London, UK", countries=["UK"], remote=True,
                employment_type="full-time", salary_min=70000, salary_max=110000,
                salary_currency="EUR", published_at=now - timedelta(days=5)),
            Job(id="j3", source="linkedin", title="Junior Software Engineer", company="StartupXYZ",
                description="Entry level developer role", location="Berlin, Germany", countries=["Germany"], remote=False,
                employment_type="full-time", salary_min=50000, salary_max=70000,
                salary_currency="EUR", published_at=now - timedelta(days=1)),
        ]

        # COLLECT (simulated)
        print(f"COLLECT: {len(jobs)} jobs")

        # NORMALIZE
        normalized = normalize_jobs("all", jobs)
        print(f"NORMALIZE: {len(normalized)} jobs")

        # VALIDATE
        valid, errors = validate_jobs(normalized)
        print(f"VALIDATE: {len(valid)} valid, {len(errors)} errors")

        # DEDUPLICATE
        deduped, dupes = deduplicate_jobs(valid)
        print(f"DEDUPLICATE: removed {dupes} duplicates")

        # FILTER
        config_path = "config/filters.yaml"
        if Path(config_path).exists():
            filters_config = load_filters(config_path)
            filtered, rejected = filter_jobs(deduped, filters_config)
            print(f"FILTER: {len(filtered)} passed, {rejected} rejected")
        else:
            filtered = deduped

        # SCORE
        scoring_path = "config/scoring.yaml"
        if Path(scoring_path).exists():
            scoring_config = load_scoring_config(scoring_path)
            scored = score_jobs(filtered, scoring_config)
            print(f"SCORE: {len(scored)} jobs scored")
            scored.sort(key=lambda x: x[1], reverse=True)
        else:
            scored = [(j, 0.5) for j in filtered]

        # STORE
        storage = Storage(db_path=str(Path(temp_db)))
        storage.init_db()
        stored_count = 0
        for job, _ in scored:
            storage.upsert_job(job)
            stored_count += 1
        storage.close()

        assert storage.count_jobs() == stored_count
        print(f"STORE: {stored_count} jobs stored")
        print(f"\n[Phase 1] COMPLETE: {stored_count} jobs stored in database")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
