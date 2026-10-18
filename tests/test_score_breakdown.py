"""Tests for score_jobs_with_breakdown (backward-compatible addition to scoring.py)."""

from __future__ import annotations


from src.models.job import Job
from src.pipeline.scoring import score_job, score_jobs, score_jobs_with_breakdown


def _job(jid: str = "j1", title: str = "Junior Developer", remote: bool = True) -> Job:
    from datetime import UTC, datetime
    return Job(
        id=jid,
        source="test",
        title=title,
        location="France",
        countries=["France"],
        remote=remote,
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


_BASE_CONFIG = {
    "scoring": {
        "location_fit": {"weight": 0.30, "target_department": "83", "preferred_countries": ["france"]},
        "domain_relevance": {"weight": 0.25},
        "seniority_match": {"weight": 0.15, "preferred_level_keywords": ["junior"], "junior_bonus": 1.2},
        "experience_fit": {"weight": 0.10},
        "skills_match": {"weight": 0.20},
    }
}


class TestScoreJobsWithBreakdown:
    def test_returns_three_element_tuples(self) -> None:
        result = score_jobs_with_breakdown([_job()], _BASE_CONFIG)
        assert len(result) == 1
        job, score, factors = result[0]
        assert isinstance(job, Job)
        assert isinstance(score, float)
        assert isinstance(factors, dict)

    def test_score_matches_score_job(self) -> None:
        """score_jobs_with_breakdown must produce the same score as score_job."""
        job = _job()
        expected = score_job(job, _BASE_CONFIG)
        _, actual, _ = score_jobs_with_breakdown([job], _BASE_CONFIG)[0]
        assert abs(actual - expected) < 1e-9

    def test_score_matches_score_jobs(self) -> None:
        """Scores from score_jobs_with_breakdown equal those from score_jobs."""
        jobs = [_job("a"), _job("b", remote=False), _job("c", title="Senior Dev")]
        expected = {j.id: s for j, s in score_jobs(jobs, _BASE_CONFIG)}
        for job, score, _ in score_jobs_with_breakdown(jobs, _BASE_CONFIG):
            assert abs(score - expected[job.id]) < 1e-9

    def test_factors_contain_expected_keys(self) -> None:
        _, _, factors = score_jobs_with_breakdown([_job()], _BASE_CONFIG)[0]
        expected_keys = {
            "location_fit", "domain_relevance", "skills_match",
            "seniority_match", "experience_fit",
        }
        assert expected_keys.issubset(factors.keys())

    def test_factor_values_are_floats_in_range(self) -> None:
        _, _, factors = score_jobs_with_breakdown([_job()], _BASE_CONFIG)[0]
        for key, val in factors.items():
            assert isinstance(val, float), f"{key} is not a float"
            assert 0.0 <= val <= 1.0, f"{key}={val} out of range"

    def test_sorted_descending(self) -> None:
        from datetime import UTC, datetime
        high = Job(id="h", source="t", title="Junior Developer", remote=True,
                   discovered_at=datetime(2026, 1, 1, tzinfo=UTC))
        low = Job(id="l", source="t", title="Senior Director VP", remote=False,
                  discovered_at=datetime(2020, 1, 1, tzinfo=UTC))
        result = score_jobs_with_breakdown([low, high], _BASE_CONFIG)
        scores = [s for _, s, _ in result]
        assert scores == sorted(scores, reverse=True)

    def test_empty_list(self) -> None:
        assert score_jobs_with_breakdown([], _BASE_CONFIG) == []

    def test_existing_score_job_unchanged(self) -> None:
        """Verify score_job still returns a plain float (no regression)."""
        result = score_job(_job(), _BASE_CONFIG)
        assert isinstance(result, float)

    def test_existing_score_jobs_unchanged(self) -> None:
        """Verify score_jobs still returns list[tuple[Job, float]] (no regression)."""
        result = score_jobs([_job()], _BASE_CONFIG)
        assert len(result) == 1
        job, score = result[0]
        assert isinstance(job, Job)
        assert isinstance(score, float)
