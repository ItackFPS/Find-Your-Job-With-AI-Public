"""Integration tests for the full Phase 1 pipeline: COLLECT → NORMALIZE → VALIDATE → DEDUPLICATE → FILTER → SCORE → STORE."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from src.models.source import Source
from src.storage.sqlite import Storage

# -- Fixtures ---------------------------------------------------------------

@pytest.fixture
def temp_db():
    """Create a unique temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield str(db_path)
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


@pytest.fixture
def sample_jobs() -> list[Job]:
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
def sample_sources() -> list[Source]:
    """Create sample source configurations."""
    return [
        Source(name="greenhouse-public", type="greenhouse", active=True),
        Source(name="ashby-public", type="ashby", active=True),
        Source(name="weworkremotely-rss", type="rss", active=True),
    ]


# -- Test: normalize_jobs ---------------------------------------------------

class TestNormalizeJobs:
    def test_normalize_single_job(self, temp_db):
        """normalize_jobs should return a list of normalized Job objects."""
        from src.pipeline.normalize import normalize_jobs

        raw = [
            {
                "title": "Test Engineer",
                "company": "Test Corp",
                "description": "A test job description",
                "location": "Paris, France",
                "country": "France",
                "remote": True,
                "salary_min": 80000,
                "salary_max": 120000,
            }
        ]

        jobs = normalize_jobs("test_source", raw)
        assert len(jobs) == 1
        job = jobs[0]
        assert isinstance(job, Job)
        assert job.title == "Test Engineer"
        assert job.company == "Test Corp"
        assert job.remote is True

    def test_normalize_batch_jobs(self, temp_db):
        """normalize_jobs should handle a batch of raw data."""
        from src.pipeline.normalize import normalize_jobs

        raw = [
            {
                "title": f"Job {i}",
                "company": f"Company {i}",
                "description": f"Description for job {i}",
                "location": "Paris, France",
                "country": "France",
                "remote": i % 2 == 0,
            }
            for i in range(5)
        ]

        jobs = normalize_jobs("test_source", raw)
        assert len(jobs) == 5
        assert all(isinstance(j, Job) for j in jobs)


# -- Test: validate_jobs ----------------------------------------------------

class TestValidateJobs:
    def test_validate_valid_job(self):
        """A job with title and company should pass validation."""
        from src.pipeline.validate import validate_jobs

        job = Job(
            id="j1", source="greenhouse", title="Software Engineer",
            company="Acme Corp", description="Backend role",
            location="Paris, France", countries=["France"], remote=True,
            employment_type="full-time", salary_min=80000,
        )

        valid, errors = validate_jobs([job])
        assert len(valid) == 1
        assert len(errors) == 0

    def test_validate_missing_title(self):
        """A job with empty title should fail validation."""
        from src.pipeline.validate import validate_jobs

        job = Job(
            id="j1", source="greenhouse", title="", company="Acme Corp",
            description="Backend role", location="Paris, France", countries=["France"],
        )

        valid, errors = validate_jobs([job])
        assert len(valid) == 0
        assert any("Title is empty" in e for e in errors)

    def test_validate_missing_company(self):
        """A job with empty company should fail validation."""
        from src.pipeline.validate import validate_jobs

        job = Job(
            id="j1", source="greenhouse", title="Software Engineer",
            company="", description="Backend role", location="Paris, France", countries=["France"],
        )

        valid, errors = validate_jobs([job])
        assert len(valid) == 0
        assert any("Company" in e for e in errors)


# -- Test: deduplicate_jobs -------------------------------------------------

class TestDeduplicateJobs:
    def test_intra_source_dedup(self):
        """Intra-source dedup should remove duplicate (source, source_job_id)."""
        from src.pipeline.deduplicate import intra_source_deduplicate

        jobs = [
            Job(id="j1", source="greenhouse", title="Job 1", source_job_id="abc"),
            Job(id="j2", source="greenhouse", title="Job 2", source_job_id="def"),
            Job(id="j3", source="greenhouse", title="Job 1", source_job_id="abc"),  # Same source_job_id
        ]

        unique = intra_source_deduplicate(jobs)
        assert len(unique) == 2

    def test_cross_source_dedup(self):
        """Cross-source dedup should remove near-duplicates."""
        from src.pipeline.deduplicate import cross_source_deduplicate

        jobs = [
            Job(id="j1", source="greenhouse", title="Software Engineer", company="Acme"),
            Job(id="j2", source="ashby", title="Software Engineer", company="Acme"),
            Job(id="j3", source="linkedin", title="Frontend Dev", company="TechStart"),
        ]

        unique = cross_source_deduplicate(jobs)
        # j1 and j2 have same company+title, so one should be removed
        assert len(unique) <= 2


# -- Test: filter_jobs ------------------------------------------------------

class TestFilterJobs:
    def test_filter_by_location(self):
        """Filter should reject jobs outside allowed locations."""
        from src.pipeline.filter import filter_jobs

        config = {
            "locations": {"include": ["France", "UK"]},
            "remote": {"allowed": []},  # No restriction
            "seniority": {"include": []},  # No restriction
            "posted_within_days": 30,
            "salary_requirements": {},
            "title_keywords": {"include": [], "exclude": []},
        }

        jobs = [
            Job(id="j1", source="greenhouse", title="Engineer", company="Acme",
                location="Paris, France", countries=["France"], remote=True),
            Job(id="j2", source="ashby", title="Developer", company="Tech",
                location="New York, US", countries=["US"], remote=False),
        ]

        valid, rejected = filter_jobs(jobs, config)
        assert len(valid) == 1
        assert len(rejected) == 1
        assert valid[0].location == "Paris, France"

    def test_filter_by_remote(self):
        """Filter should reject non-remote jobs when only remote is allowed."""
        from src.pipeline.filter import filter_jobs

        config = {
            "locations": {"include": []},
            "remote": {"allowed": ["remote"]},
            "seniority": {"include": []},
            "posted_within_days": 30,
            "salary_requirements": {},
            "title_keywords": {"include": [], "exclude": []},
        }

        jobs = [
            Job(id="j1", source="greenhouse", title="Engineer", company="Acme",
                location="", countries=[], remote=True),
            Job(id="j2", source="ashby", title="Developer", company="Tech",
                location="", countries=[], remote=False),
        ]

        valid, rejected = filter_jobs(jobs, config)
        assert len(valid) == 1
        assert len(rejected) == 1


# -- Test: score_jobs -------------------------------------------------------

class TestScoreJobs:
    def test_score_single_job(self):
        """Scoring should return a list of (job, score) tuples."""
        from src.pipeline.scoring import score_jobs

        config = {
            "scoring": {
                "role_match": {"weight": 0.3, "keywords": ["engineer"]},
                "remote_compatibility": {"weight": 0.2, "bonus_remote": 1.5, "penalty_hybrid": 0.8},
                "location_match": {"weight": 0.2, "exact_location_bonus": 1.3},
                "seniority_match": {"weight": 0.15, "junior_bonus": 1.2},
                "recency": {"weight": 0.15, "decay_days": 7},
                "skills_match": {"weight": 0.1, "skill_bonus": 0.1},
            }
        }

        job = Job(
            id="j1", source="greenhouse", title="Software Engineer", company="Acme",
            location="Paris, France", countries=["France"], remote=True,
            employment_type="full-time", salary_min=80000, salary_max=120000,
        )

        scored = score_jobs([job], config)
        assert len(scored) == 1
        job_result, score = scored[0]
        assert isinstance(job_result, Job)
        assert 0.0 <= score <= 1.0


# -- Test: full pipeline integration ----------------------------------------

class TestFullPipeline:
    """End-to-end test of the complete Phase 1 pipeline."""

    def test_full_pipeline(self, temp_db):
        """Run the complete pipeline: COLLECT → NORMALIZE → VALIDATE → DEDUPLICATE → FILTER → SCORE → STORE."""
        from src.models.job import Job
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filters
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.validate import validate_jobs

        # 1. Create sample jobs (simulating collected data)
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
            Job(id="j4", source="greenhouse", title="Software Engineer", company="Acme Corp",
                description="Backend developer role", location="Paris, France", countries=["France"], remote=True,
                employment_type="full-time", salary_min=80000, salary_max=120000,
                salary_currency="EUR", published_at=now - timedelta(days=3)),  # Duplicate
            Job(id="j5", source="remote", title="Contract Developer", company="FreelanceHub",
                description="Freelance contract only", location="", countries=[], remote=True,
                employment_type="contract", salary_min=60000, salary_max=None,
                salary_currency="EUR", published_at=now - timedelta(days=14)),  # Old + excluded keyword
        ]

        print(f"\n[Phase 1] COLLECT: {len(jobs)} jobs collected")

        # 2. Normalize
        normalized = normalize_jobs("all", jobs)
        print(f"[Phase 1] NORMALIZE: {len(normalized)} jobs normalized")

        # 3. Validate
        valid, errors = validate_jobs(normalized)
        print(f"[Phase 1] VALIDATE: {len(valid)} valid, {len(errors)} errors")

        # 4. Deduplicate
        deduped, duplicates_removed = deduplicate_jobs(valid)
        print(f"[Phase 1] DEDUPLICATE: removed {duplicates_removed} duplicates, {len(deduped)} remaining")

        # 5. Filter
        config_path = "config/filters.yaml"
        if Path(config_path).exists():
            filters_config = load_filters(config_path)
            filtered, rejected = filter_jobs(deduped, filters_config)
            print(f"[Phase 1] FILTER: {len(filtered)} passed, {rejected} rejected")
        else:
            print("[Phase 1] FILTER: config not found, skipping")
            filtered = deduped

        # 6. Score
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

        # 7. Store
        storage = Storage(db_path=str(Path(temp_db)))
        storage.init_db()
        stored_count = 0
        for job, _ in scored:
            storage.upsert_job(job)
            stored_count += 1
        print(f"[Phase 1] STORE: {stored_count} jobs stored")

        # Verify storage
        total_stored = storage.count_jobs()
        assert total_stored == stored_count, f"Expected {stored_count} jobs in DB, got {total_stored}"

        # Verify all scored jobs are retrievable
        retrieved = storage.get_jobs(limit=len(scored))
        assert len(retrieved) >= len(scored), f"Expected at least {len(scored)} jobs, got {len(retrieved)}"

        storage.close()

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
