"""Unit tests for the per-source rejection funnel introduced in the orchestrator.

These tests verify that by_source stats are populated with the expected
funnel fields (validated, unique, passed_filter, rejection_reasons) when
jobs pass through the validate → deduplicate → filter pipeline stages.

No network access. No real database. No embeddings.
Tests exercise the counting logic directly against the pipeline stages.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from src.models.job import Job
from tests.fixtures.profiles import TEST_PROFILE_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _job(
    job_id: str,
    source: str,
    title: str = "Developer",
    location: str = "Paris, France",
    countries: list[str] | None = None,
    remote: bool = True,
    published_at_offset_days: int = 1,
) -> Job:
    return Job(
        id=job_id,
        source=source,
        source_job_id=job_id,
        title=title,
        company="Test Corp",
        description=f"Python developer role at {source}",
        location=location,
        countries=countries if countries is not None else ["France"],
        remote=remote,
        employment_type="full-time",
        published_at=_NOW - timedelta(days=published_at_offset_days),
    )


# ---------------------------------------------------------------------------
# Tests: per-source funnel counts after validate + dedup + filter
# ---------------------------------------------------------------------------

class TestFunnelCounts:
    """Simulate the orchestrator's counting logic on controlled data."""

    def test_validated_count_per_source(self):
        from src.pipeline.validate import validate_jobs

        jobs = [
            _job("s1-j1", "source_a"),
            _job("s1-j2", "source_a"),
            _job("s2-j1", "source_b"),
        ]
        valid, _ = validate_jobs(jobs)
        by_source: dict = {
            "source_a": {"collected": 2},
            "source_b": {"collected": 1},
        }
        for job in valid:
            if job.source in by_source:
                by_source[job.source].setdefault("validated", 0)
                by_source[job.source]["validated"] += 1

        assert by_source["source_a"]["validated"] == 2
        assert by_source["source_b"]["validated"] == 1

    def test_unique_count_per_source(self):
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.validate import validate_jobs

        # source_a has 2 jobs; source_b has 1 duplicate of source_a job
        job_a1 = _job("sa-1", "source_a", title="Python Dev", location="Paris, France")
        job_a2 = _job("sa-2", "source_a", title="JS Dev", location="Paris, France")
        # Duplicate of job_a1 on source_b (same company, title, location, type)
        job_b1 = Job(
            id="sb-1",
            source="source_b",
            source_job_id="sb-1",
            title="Python Dev",
            company="Test Corp",
            description="Mirror posting",
            location="Paris, France",
            countries=["France"],
            remote=True,
            employment_type="full-time",
            published_at=_NOW - timedelta(days=1),
        )

        jobs = [job_a1, job_a2, job_b1]
        valid, _ = validate_jobs(jobs)
        deduped, removed = deduplicate_jobs(valid)

        assert removed == 1  # job_b1 deduped as mirror of job_a1

        by_source: dict = {
            "source_a": {"collected": 2},
            "source_b": {"collected": 1},
        }
        for job in deduped:
            if job.source in by_source:
                by_source[job.source].setdefault("unique", 0)
                by_source[job.source]["unique"] += 1

        assert by_source["source_a"]["unique"] == 2
        assert by_source["source_b"].get("unique", 0) == 0  # deduped away

    def test_passed_filter_and_rejection_reasons(self):
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
        from src.pipeline.orchestrator import RejectionRecord
        from src.pipeline.validate import validate_jobs

        jobs = [
            _job("pass-1", "source_it", title="Python Developer"),  # should pass
            _job("fail-1", "source_it", title="Senior Director Engineering",
                 location="Berlin, Germany", countries=["Germany"], remote=False),  # location + seniority
            _job("fail-2", "source_it", title="Comptable Senior"),   # non-IT title
        ]

        profile = load_profile(TEST_PROFILE_PATH)
        engine_cfg = load_filters("config/filters.yaml")
        filter_cfg = build_filter_config(profile, engine_cfg)

        valid, _ = validate_jobs(jobs)
        deduped, _ = deduplicate_jobs(valid)
        filtered_jobs, rejection_reasons = filter_jobs(deduped, filter_cfg)

        # Build RejectionRecords (mirrors orchestrator logic)
        deduped_map = {j.id: j for j in deduped}
        rejection_records: list[RejectionRecord] = []
        for job_id, reason in rejection_reasons.items():
            job = deduped_map.get(job_id)
            if job:
                rejection_records.append(RejectionRecord(
                    job_id=job.id,
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    source=job.source,
                    rejection_stage="filter",
                    rejection_detail=reason,
                ))

        by_source: dict = {"source_it": {"collected": 3}}

        for job in filtered_jobs:
            if job.source in by_source:
                by_source[job.source].setdefault("passed_filter", 0)
                by_source[job.source]["passed_filter"] += 1

        for record in rejection_records:
            src = record.source or "unknown"
            if src in by_source:
                rejections = by_source[src].setdefault("rejection_reasons", {})
                reason = record.rejection_detail or "unknown"
                rejections[reason] = rejections.get(reason, 0) + 1

        src_stats = by_source["source_it"]
        assert src_stats["passed_filter"] == 1
        assert "rejection_reasons" in src_stats
        # fail-1 rejected by location (Germany non-remote → location check first)
        # fail-2 rejected by title_keywords or seniority
        total_rejections = sum(src_stats["rejection_reasons"].values())
        assert total_rejections == 2

    def test_no_rejections_no_rejection_reasons_key(self):
        """Sources with zero rejections must not have a rejection_reasons key."""
        from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
        from src.pipeline.validate import validate_jobs

        jobs = [_job("ok-1", "clean_source", title="Python Developer")]

        profile = load_profile(TEST_PROFILE_PATH)
        engine_cfg = load_filters("config/filters.yaml")
        filter_cfg = build_filter_config(profile, engine_cfg)

        valid, _ = validate_jobs(jobs)
        filtered_jobs, rejection_reasons = filter_jobs(valid, filter_cfg)

        by_source: dict = {"clean_source": {"collected": 1}}

        for job in filtered_jobs:
            if job.source in by_source:
                by_source[job.source].setdefault("passed_filter", 0)
                by_source[job.source]["passed_filter"] += 1

        # No rejections — rejection_reasons should not be added
        assert "rejection_reasons" not in by_source["clean_source"]
        assert by_source["clean_source"]["passed_filter"] == 1

    def test_multi_source_rejection_isolation(self):
        """Rejections from source_a must not pollute source_b counters."""
        from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
        from src.pipeline.orchestrator import RejectionRecord
        from src.pipeline.validate import validate_jobs

        jobs = [
            _job("a-1", "source_a", title="Python Developer"),     # passes
            _job("a-2", "source_a", title="Comptable Senior"),      # rejected
            _job("b-1", "source_b", title="JavaScript Developer"),  # passes
        ]

        profile = load_profile(TEST_PROFILE_PATH)
        engine_cfg = load_filters("config/filters.yaml")
        filter_cfg = build_filter_config(profile, engine_cfg)

        valid, _ = validate_jobs(jobs)
        filtered_jobs, rejection_reasons = filter_jobs(valid, filter_cfg)

        deduped_map = {j.id: j for j in valid}
        rejection_records: list[RejectionRecord] = []
        for job_id, reason in rejection_reasons.items():
            job = deduped_map.get(job_id)
            if job:
                rejection_records.append(RejectionRecord(
                    job_id=job.id, title=job.title, company=job.company,
                    location=job.location, source=job.source,
                    rejection_stage="filter", rejection_detail=reason,
                ))

        by_source: dict = {
            "source_a": {"collected": 2},
            "source_b": {"collected": 1},
        }

        for job in filtered_jobs:
            if job.source in by_source:
                by_source[job.source].setdefault("passed_filter", 0)
                by_source[job.source]["passed_filter"] += 1

        for record in rejection_records:
            src = record.source or "unknown"
            if src in by_source:
                rejections = by_source[src].setdefault("rejection_reasons", {})
                reason = record.rejection_detail or "unknown"
                rejections[reason] = rejections.get(reason, 0) + 1

        assert by_source["source_a"]["passed_filter"] == 1
        assert sum(by_source["source_a"]["rejection_reasons"].values()) == 1
        assert by_source["source_b"]["passed_filter"] == 1
        assert "rejection_reasons" not in by_source["source_b"]


# ---------------------------------------------------------------------------
# Tests: RejectionRecord structure
# ---------------------------------------------------------------------------

class TestRejectionRecord:
    def test_rejection_record_has_required_fields(self):
        from src.pipeline.orchestrator import RejectionRecord

        r = RejectionRecord(
            job_id="test-id",
            title="Some Job",
            company="Corp",
            location="Paris",
            source="arbeitnow-public",
            rejection_stage="filter",
            rejection_detail="location",
        )
        assert r.rejection_detail == "location"
        assert r.source == "arbeitnow-public"
        assert r.rejection_stage == "filter"

    def test_rejection_record_allows_none_fields(self):
        from src.pipeline.orchestrator import RejectionRecord

        r = RejectionRecord(
            job_id=None,
            title=None,
            company=None,
            location=None,
            source=None,
            rejection_stage="filter",
            rejection_detail=None,
        )
        assert r.job_id is None
        assert r.source is None
