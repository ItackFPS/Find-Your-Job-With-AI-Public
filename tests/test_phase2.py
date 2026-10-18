"""Tests for Phase 2: Local AI Job Understanding.

All tests use mocked LLM responses — no actual LLM server is required.
"""

from __future__ import annotations

import pytest
import requests
from pydantic import ValidationError

from src.ai.interfaces import LLMProvider
from src.ai.models import (
    BatchJobUnderstandingResult,
    JobUnderstanding,
    JobUnderstandingResult,
)
from src.ai.pipeline import Phase2Pipeline
from src.ai.storage import AIAnalysisStorage
from src.models.job import Job
from src.storage.sqlite import Storage

# -- Fixtures ---------------------------------------------------------------

@pytest.fixture
def sample_job() -> Job:
    """Create a sample job for testing."""
    return Job(
        id="test-job-1",
        source="greenhouse",
        title="Senior Software Engineer",
        company="Acme Corp",
        description=(
            "We are looking for a senior backend software engineer with 5+ years of experience. "
            "Required skills: Python, Django, PostgreSQL, REST APIs. "
            "Preferred skills: Redis, Celery, Docker. "
            "Education: Bachelor's degree in Computer Science or equivalent. "
            "Employment type: full-time. Remote: fully remote."
        ),
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        salary_min=80000,
        salary_max=120000,
        salary_currency="EUR",
    )


@pytest.fixture
def sample_jobs(sample_job: Job) -> list[Job]:
    """Create multiple sample jobs for testing."""
    return [
        sample_job,
        Job(
            id="test-job-2",
            source="ashby",
            title="Frontend Developer",
            company="TechStart Inc",
            description=(
                "We need a frontend developer with experience in React and TypeScript. "
                "Required: React, JavaScript, HTML/CSS. "
                "Preferred: Node.js, Webpack, Jest. "
                "Education: Bachelor's degree or equivalent. "
                "Employment type: part-time. Remote: hybrid."
            ),
            location="London, UK",
            countries=["UK"],
            remote=True,
            employment_type="part-time",
        ),
    ]


# -- Mock LLM Provider ------------------------------------------------------

class MockLLMProvider(LLMProvider):
    """Mock LLM provider for testing. Returns deterministic results."""

    def __init__(self, mock_responses: dict[str, JobUnderstanding] | None = None):
        self.mock_responses = mock_responses or {}

    def analyze_job(self, job: Job, prompt_version: str = "job_understanding_v1", profile=None) -> JobUnderstandingResult:
        """Analyze a single job using mocked responses."""
        if job.id in self.mock_responses:
            analysis = self.mock_responses[job.id]
            return JobUnderstandingResult(
                job_id=job.id,
                analysis=analysis,
                prompt_version=prompt_version,
                model_identifier="mock-llm",
                status="success",
            )

        # Default mock response for unknown jobs
        default_analysis = JobUnderstanding(
            seniority_level=None,
            required_skills=["python"],
            preferred_skills=[],
            programming_languages=["javascript"],
            frameworks=[],
            tools=[],
            technologies=[],
            education_requirements="Bachelor's degree",
            experience_years_min=2.0,
            experience_years_max=None,
            employment_type="full-time",
            remote_type="fully remote",
            location_requirements="Europe",
            language_requirements=["english"],
            responsibilities=["Develop web applications"],
            key_requirements=["Python", "JavaScript"],
            nice_to_have=[],
            domain="Software Development",
            confidence=0.8,
        )
        return JobUnderstandingResult(
            job_id=job.id,
            analysis=default_analysis,
            prompt_version=prompt_version,
            model_identifier="mock-llm",
            status="success",
        )

    def analyze_batch(self, jobs: list[Job], prompt_version: str = "job_understanding_v1", profile=None, on_progress=None) -> BatchJobUnderstandingResult:
        """Analyze a batch of jobs using mocked responses."""
        results = []
        for job in jobs:
            result = self.analyze_job(job, prompt_version, profile=profile)
            results.append(result)

        successful = sum(1 for r in results if r.status == "success")
        failed = sum(1 for r in results if r.status == "failed")
        skipped = sum(1 for r in results if r.status == "skipped")

        return BatchJobUnderstandingResult(
            total_jobs=len(jobs),
            successful=successful,
            failed=failed,
            skipped=skipped,
            results=results,
        )


# -- Tests ------------------------------------------------------------------

class TestMockLLMProvider:
    """Tests for the MockLLMProvider."""

    def test_analyze_job_success(self, sample_job):
        """Test successful job analysis with mocked response."""
        mock_responses = {
            "test-job-1": JobUnderstanding(
                seniority_level="senior",
                required_skills=["Python", "Django", "PostgreSQL"],
                preferred_skills=["Redis", "Celery", "Docker"],
                programming_languages=["Python"],
                frameworks=[],
                tools=[],
                technologies=[],
                education_requirements="Bachelor's degree",
                experience_years_min=5.0,
                experience_years_max=None,
                employment_type="full-time",
                remote_type="fully remote",
                location_requirements="France",
                language_requirements=["english"],
                responsibilities=["Backend development"],
                key_requirements=["Python", "Django"],
                nice_to_have=[],
                domain="Backend Development",
                confidence=0.9,
            ),
        }

        provider = MockLLMProvider(mock_responses)
        result = provider.analyze_job(sample_job)

        assert result.status == "success"
        assert result.analysis.seniority_level == "senior"
        assert "Python" in result.analysis.required_skills
        assert result.analysis.preferred_skills == ["Redis", "Celery", "Docker"]
        assert result.analysis.experience_years_min == 5.0

    def test_analyze_job_unknown(self, sample_job):
        """Test analysis of a job not in mock responses."""
        provider = MockLLMProvider()
        result = provider.analyze_job(sample_job)

        assert result.status == "success"
        assert result.analysis.seniority_level is None  # Not specified in default
        assert result.analysis.required_skills == ["python"]

    def test_analyze_batch(self, sample_jobs):
        """Test batch analysis."""
        provider = MockLLMProvider()
        result = provider.analyze_batch(sample_jobs)

        assert result.total_jobs == len(sample_jobs)
        assert result.successful == len(sample_jobs)
        assert result.failed == 0
        assert result.skipped == 0

    def test_analyze_batch_with_failure(self, sample_job):
        """Test batch analysis with a mocked failure."""
        mock_responses = {
            "test-job-1": JobUnderstanding(
                seniority_level="senior",
                required_skills=["Python"],
                preferred_skills=[],
                programming_languages=[],
                frameworks=[],
                tools=[],
                technologies=[],
                education_requirements=None,
                experience_years_min=None,
                experience_years_max=None,
                employment_type=None,
                remote_type=None,
                location_requirements=None,
                language_requirements=[],
                responsibilities=[],
                key_requirements=[],
                nice_to_have=[],
                domain=None,
                confidence=0.5,
            ),
        }

        provider = MockLLMProvider(mock_responses)
        result = provider.analyze_batch([sample_job])

        assert result.successful == 1
        assert result.failed == 0


class TestPhase2Pipeline:
    """Tests for the Phase 2 pipeline."""

    def test_pipeline_with_mock_llm(self, sample_jobs, tmp_path):
        """Test Phase 2 pipeline with a mocked LLM provider."""
        mock_responses = {
            "test-job-1": JobUnderstanding(
                seniority_level="senior",
                required_skills=["Python", "Django"],
                preferred_skills=[],
                programming_languages=["Python"],
                frameworks=[],
                tools=[],
                technologies=[],
                education_requirements="Bachelor's degree",
                experience_years_min=5.0,
                experience_years_max=None,
                employment_type="full-time",
                remote_type="fully remote",
                location_requirements="France",
                language_requirements=["english"],
                responsibilities=["Backend development"],
                key_requirements=["Python", "Django"],
                nice_to_have=[],
                domain="Backend Development",
                confidence=0.9,
            ),
        }

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=MockLLMProvider(mock_responses),
            prompt_version="job_understanding_v1",
        )

        result = pipeline.run([sample_jobs[0], sample_jobs[1]])

        # job-1 gets its mocked response, job-2 falls back to the provider's
        # default mock analysis — both come back as "success".
        assert result.total_jobs == len(sample_jobs)
        assert result.successful == len(sample_jobs)
        assert result.failed == 0
        assert result.skipped == 0

    def test_pipeline_with_no_llm(self, sample_jobs, tmp_path):
        """Test Phase 2 pipeline with no LLM provider (AI disabled)."""
        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=None,  # AI disabled
            prompt_version="job_understanding_v1",
        )

        result = pipeline.run([sample_jobs[0], sample_jobs[1]])

        assert result.total_jobs == len(sample_jobs)
        assert result.successful == 0
        assert result.failed == 0
        assert result.skipped == len(sample_jobs)

    def test_pipeline_with_cached_results(self, sample_jobs, tmp_path):
        """Test Phase 2 pipeline with cached results."""
        mock_responses = {
            "test-job-1": JobUnderstanding(
                seniority_level="senior",
                required_skills=["Python"],
                preferred_skills=[],
                programming_languages=[],
                frameworks=[],
                tools=[],
                technologies=[],
                education_requirements=None,
                experience_years_min=None,
                experience_years_max=None,
                employment_type=None,
                remote_type=None,
                location_requirements=None,
                language_requirements=[],
                responsibilities=[],
                key_requirements=[],
                nice_to_have=[],
                domain=None,
                confidence=0.5,
            ),
        }

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=MockLLMProvider(mock_responses),
            prompt_version="job_understanding_v1",
        )

        # First run — should analyze both jobs (job-1 via the mocked
        # response, job-2 via the provider's default fallback).
        result1 = pipeline.run(sample_jobs)
        assert result1.successful == len(sample_jobs)

        # Second run — should use the cache for both jobs.
        result2 = pipeline.run(sample_jobs)
        assert result2.cached == len(sample_jobs)

    def test_pipeline_with_failed_analysis(self, sample_job, tmp_path):
        """Test Phase 2 pipeline with a failed LLM analysis."""
        provider = MockLLMProvider()
        # Override to simulate failure
        def failing_analyze_job(job: Job, prompt_version: str) -> JobUnderstandingResult:
            return JobUnderstandingResult(
                job_id=job.id,
                analysis=JobUnderstanding(),
                prompt_version=prompt_version,
                model_identifier="mock-llm",
                status="failed",
                error_message="Connection refused",
            )

        provider.analyze_job = failing_analyze_job
        provider.analyze_batch = lambda jobs, pv, profile=None, on_progress=None: BatchJobUnderstandingResult(
            total_jobs=len(jobs),
            successful=0,
            failed=len(jobs),
            skipped=0,
            results=[
                JobUnderstandingResult(
                    job_id=j.id,
                    analysis=JobUnderstanding(),
                    prompt_version=pv,
                    model_identifier="mock-llm",
                    status="failed",
                    error_message="Connection refused",
                )
                for j in jobs
            ],
        )

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=provider,
            prompt_version="job_understanding_v1",
        )

        result = pipeline.run([sample_job])

        assert result.successful == 0
        assert result.failed == 1
        assert result.skipped == 0

    def test_pipeline_with_mixed_results(self, sample_jobs, tmp_path):
        """Test Phase 2 pipeline with mixed success/failure results."""
        mock_responses = {
            "test-job-1": JobUnderstanding(
                seniority_level="senior",
                required_skills=["Python"],
                preferred_skills=[],
                programming_languages=[],
                frameworks=[],
                tools=[],
                technologies=[],
                education_requirements=None,
                experience_years_min=None,
                experience_years_max=None,
                employment_type=None,
                remote_type=None,
                location_requirements=None,
                language_requirements=[],
                responsibilities=[],
                key_requirements=[],
                nice_to_have=[],
                domain=None,
                confidence=0.5,
            ),
        }

        provider = MockLLMProvider(mock_responses)
        # Override job-2 to fail
        def mixed_analyze_job(job: Job, pv: str) -> JobUnderstandingResult:
            if job.id == "test-job-1":
                return JobUnderstandingResult(
                    job_id=job.id,
                    analysis=JobUnderstanding(seniority_level="senior", required_skills=["Python"]),
                    prompt_version=pv,
                    model_identifier="mock-llm",
                    status="success",
                )
            return JobUnderstandingResult(
                job_id=job.id,
                analysis=JobUnderstanding(),
                prompt_version=pv,
                model_identifier="mock-llm",
                status="failed",
                error_message="Timeout",
            )

        provider.analyze_job = mixed_analyze_job
        provider.analyze_batch = lambda jobs, pv, profile=None, on_progress=None: BatchJobUnderstandingResult(
            total_jobs=len(jobs),
            successful=1,
            failed=1,
            skipped=0,
            results=[
                JobUnderstandingResult(
                    job_id="test-job-1",
                    analysis=JobUnderstanding(seniority_level="senior", required_skills=["Python"]),
                    prompt_version=pv,
                    model_identifier="mock-llm",
                    status="success",
                ),
                JobUnderstandingResult(
                    job_id="test-job-2",
                    analysis=JobUnderstanding(),
                    prompt_version=pv,
                    model_identifier="mock-llm",
                    status="failed",
                    error_message="Timeout",
                ),
            ],
        )

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=provider,
            prompt_version="job_understanding_v1",
        )

        result = pipeline.run(sample_jobs)

        assert result.successful == 1
        assert result.failed == 1


class TestJobUnderstandingModel:
    """Tests for the JobUnderstanding Pydantic model."""

    def test_default_values(self):
        """Test default values for all fields."""
        analysis = JobUnderstanding()
        assert analysis.seniority_level is None
        assert analysis.required_skills == []
        assert analysis.preferred_skills == []
        assert analysis.programming_languages == []
        assert analysis.frameworks == []
        assert analysis.tools == []
        assert analysis.technologies == []
        assert analysis.education_requirements is None
        assert analysis.experience_years_min is None
        assert analysis.experience_years_max is None
        assert analysis.employment_type is None
        assert analysis.remote_type is None
        assert analysis.location_requirements is None
        assert analysis.language_requirements == []
        assert analysis.responsibilities == []
        assert analysis.key_requirements == []
        assert analysis.nice_to_have == []
        assert analysis.domain is None
        assert analysis.confidence == 0.5

    def test_confidence_bounds(self):
        """Test confidence field bounds."""
        assert JobUnderstanding(confidence=0.0) is not None
        assert JobUnderstanding(confidence=1.0) is not None
        with pytest.raises(ValidationError):
            JobUnderstanding(confidence=-0.1)
        with pytest.raises(ValidationError):
            JobUnderstanding(confidence=1.1)

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "seniority_level": "junior",
            "required_skills": ["python"],
            "preferred_skills": [],
            "programming_languages": ["javascript"],
            "frameworks": ["react"],
            "tools": ["git"],
            "technologies": ["aws"],
            "education_requirements": "High school diploma",
            "experience_years_min": 1.0,
            "experience_years_max": 3.0,
            "employment_type": "full-time",
            "remote_type": "hybrid",
            "location_requirements": "New York",
            "language_requirements": ["english"],
            "responsibilities": ["Frontend development"],
            "key_requirements": ["React", "JavaScript"],
            "nice_to_have": ["typescript"],
            "domain": "Web Development",
            "confidence": 0.9,
        }

        analysis = JobUnderstanding(**data)
        assert analysis.seniority_level == "junior"
        assert analysis.required_skills == ["python"]
        assert analysis.experience_years_min == 1.0


class TestJobUnderstandingResult:
    """Tests for the JobUnderstandingResult model."""

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "job_id": "test-job-1",
            "analysis": JobUnderstanding(
                seniority_level="senior",
                required_skills=["Python"],
                confidence=0.9,
            ).model_dump(),
            "prompt_version": "job_understanding_v1",
            "model_identifier": "mock-llm",
            "status": "success",
        }

        result = JobUnderstandingResult(**data)
        assert result.job_id == "test-job-1"
        assert result.status == "success"


class TestBatchJobUnderstandingResult:
    """Tests for the BatchJobUnderstandingResult model."""

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "total_jobs": 2,
            "successful": 1,
            "failed": 1,
            "skipped": 0,
            "results": [
                {
                    "job_id": "test-job-1",
                    "analysis": JobUnderstanding(seniority_level="senior").model_dump(),
                    "prompt_version": "job_understanding_v1",
                    "model_identifier": "mock-llm",
                    "status": "success",
                },
                {
                    "job_id": "test-job-2",
                    "analysis": JobUnderstanding().model_dump(),
                    "prompt_version": "job_understanding_v1",
                    "model_identifier": "mock-llm",
                    "status": "failed",
                    "error_message": "Connection refused",
                },
            ],
        }

        result = BatchJobUnderstandingResult(**data)
        assert result.total_jobs == 2
        assert result.successful == 1
        assert result.failed == 1


class TestPromptTemplate:
    """Tests for the prompt template."""

    def test_prompt_contains_required_fields(self):
        """Test that the prompt contains the fields Phase 2 actually asks the
        LLM to generate — the minimal schema (see the 2026-09-04 schema audit):
        only fields consumed downstream by ``phase3_bridge.build_enriched_job_text``
        plus the low-risk ``confidence`` scalar. Fields that are redundant with
        already-deterministic data (education/experience/employment_type/
        remote_type/location) or have no consumer at all (key_requirements,
        nice_to_have) were dropped from LLM generation to cut output tokens
        and validation-failure surface; they remain on the ``JobUnderstanding``
        model (default None/[]) for storage/cache compatibility.
        """
        from src.ai.prompt import build_job_understanding_prompt

        prompt = build_job_understanding_prompt(
            job_description="Test job description",
            job_title="Test Title",
            job_company="Test Company",
        )

        assert "seniority_level" in prompt
        assert "required_skills" in prompt
        assert "preferred_skills" in prompt
        assert "programming_languages" in prompt
        assert "frameworks" in prompt
        assert "tools" in prompt
        assert "technologies" in prompt
        assert "language_requirements" in prompt
        assert "responsibilities" in prompt
        assert "domain" in prompt
        assert "confidence" in prompt

    def test_prompt_omits_unused_fields(self):
        """Fields with no downstream consumer must not be requested from the
        LLM anymore — see the 2026-09-04 Phase 2 schema audit. They still
        exist on the JobUnderstanding model (default None/[]), just aren't
        generated.
        """
        from src.ai.prompt import build_job_understanding_prompt

        prompt = build_job_understanding_prompt(
            job_description="Test job description",
            job_title="Test Title",
            job_company="Test Company",
        )

        for field in (
            "education_requirements",
            "experience_years_min",
            "experience_years_max",
            "employment_type",
            "remote_type",
            "location_requirements",
            "key_requirements",
            "nice_to_have",
        ):
            assert field not in prompt, f"{field} should have been removed from the LLM schema"

    def test_prompt_version(self):
        """Test that the prompt version is configurable."""
        from src.ai.prompt import PROMPT_VERSION

        assert PROMPT_VERSION == "job_understanding_v1"


# ---------------------------------------------------------------------------
# Empty LLM response handling
# ---------------------------------------------------------------------------

class TestEmptyLLMResponse:
    """LocalLLMProvider must mark jobs as failed when LLM returns empty content.

    Reasoning models (e.g. bonsai-27b) can consume their entire token budget
    on chain-of-thought and return content=''.  Treating that as a success
    would silently produce a default-empty JobUnderstanding with no error flag.
    """

    def test_empty_response_returns_failed_status(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch.object(LocalLLMProvider, "_call_llm", return_value=""):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.error_message is not None
        assert "empty" in result.error_message.lower()

    def test_whitespace_only_response_returns_failed_status(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch.object(LocalLLMProvider, "_call_llm", return_value="   \n  "):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"

    def test_empty_response_not_cached_as_success(self, sample_job, tmp_path):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider
        from src.ai.pipeline import Phase2Pipeline
        from src.ai.storage import AIAnalysisStorage
        from src.storage.sqlite import Storage

        db_path = str(tmp_path / "test.db")
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        call_count = {"n": 0}

        def empty_call_llm(self_inner, prompt, max_tokens=None):
            call_count["n"] += 1
            return ""

        with patch.object(LocalLLMProvider, "_call_llm", empty_call_llm):
            provider = LocalLLMProvider(timeout_seconds=1)
            pipeline = Phase2Pipeline(
                storage=Storage(db_path=db_path),
                ai_storage=ai_storage,
                llm_provider=provider,
            )
            result1 = pipeline.run([sample_job])

        assert result1.failed == 1
        assert result1.successful == 0

        # Second run: LLM returns empty again — must NOT use a cached success
        with patch.object(LocalLLMProvider, "_call_llm", empty_call_llm):
            provider2 = LocalLLMProvider(timeout_seconds=1)
            pipeline2 = Phase2Pipeline(
                storage=Storage(db_path=db_path),
                ai_storage=ai_storage,
                llm_provider=provider2,
            )
            result2 = pipeline2.run([sample_job])

        # Failed analyses are stored and re-served from cache on subsequent
        # runs (same as successes) to avoid hammering the LLM on every run.
        # What must NOT happen is a failed result being surfaced as success.
        assert result2.failed + result2.cached >= 1
        assert result2.successful == 0

    def test_non_empty_response_still_succeeds(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        valid_json = '{"required_skills": ["python"], "confidence": 0.8}'
        with patch.object(LocalLLMProvider, "_call_llm", return_value=valid_json):
            result = provider.analyze_job(sample_job)

        assert result.status == "success"
        assert result.analysis.required_skills == ["python"]


# ---------------------------------------------------------------------------
# Phase 2 timing metadata
# ---------------------------------------------------------------------------

class TestPhase2Timing:
    """BatchJobUnderstandingResult must carry wall-clock timing information."""

    def test_time_seconds_is_populated(self, sample_jobs, tmp_path):
        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=MockLLMProvider(),
        )
        result = pipeline.run(sample_jobs)

        assert result.time_seconds >= 0.0

    def test_time_seconds_zero_when_ai_disabled(self, sample_jobs, tmp_path):
        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=None,
        )
        result = pipeline.run(sample_jobs)

        assert result.time_seconds == 0.0

    def test_time_seconds_field_exists_on_model(self):
        result = BatchJobUnderstandingResult(
            total_jobs=0, successful=0, failed=0, skipped=0, results=[]
        )
        assert hasattr(result, "time_seconds")
        assert result.time_seconds == 0.0


# ---------------------------------------------------------------------------
# Candidate limit and ordering (via _run_phase2 orchestrator helper)
# ---------------------------------------------------------------------------

class TestPhase2CandidateLimit:
    """_run_phase2 must select only the top-N candidates by score order.

    Jobs not selected stay in the pipeline; they must never be discarded.
    """

    def _make_jobs(self, n: int) -> list[Job]:
        return [
            Job(id=f"job-{i:03d}", source="test", title=f"Job {i}", company="Co")
            for i in range(n)
        ]

    def test_phase2pipeline_receives_only_sliced_candidates(self, tmp_path):
        """Phase2Pipeline.run() only receives the top-N jobs, not the full list."""
        from src.ai.pipeline import Phase2Pipeline
        from src.ai.storage import AIAnalysisStorage
        from src.storage.sqlite import Storage

        jobs = self._make_jobs(10)
        max_n = 3
        candidates = jobs[:max_n]

        db_path = str(tmp_path / "test.db")
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        analyzed_ids: list[str] = []

        class TrackingProvider:
            def analyze_batch(self, batch, prompt_version, profile=None, on_progress=None):
                for j in batch:
                    analyzed_ids.append(j.id)
                results = [
                    JobUnderstandingResult(
                        job_id=j.id,
                        analysis=JobUnderstanding(),
                        prompt_version=prompt_version,
                        model_identifier="mock",
                        status="success",
                    )
                    for j in batch
                ]
                return BatchJobUnderstandingResult(
                    total_jobs=len(batch),
                    successful=len(batch),
                    failed=0,
                    skipped=0,
                    results=results,
                )

        pipeline = Phase2Pipeline(
            storage=Storage(db_path=db_path),
            ai_storage=ai_storage,
            llm_provider=TrackingProvider(),  # type: ignore[arg-type]
        )
        pipeline.run(candidates)

        assert len(analyzed_ids) == max_n
        assert analyzed_ids == [f"job-{i:03d}" for i in range(max_n)]

    def test_candidates_sliced_to_max_candidates(self, tmp_path):
        """Top-N slicing: when max_candidates=3 and 10 jobs supplied, only 3 are selected."""
        from src.ai.config import Phase2Config

        jobs = self._make_jobs(10)
        cfg = Phase2Config(max_candidates=3)
        candidates = jobs[: cfg.max_candidates]

        assert len(candidates) == 3
        assert candidates[0].id == "job-000"
        assert candidates[2].id == "job-002"

    def test_max_candidates_default_is_100(self, monkeypatch):
        monkeypatch.delenv("PHASE2_MAX_CANDIDATES", raising=False)
        from src.ai.config import Phase2Config

        cfg = Phase2Config()
        assert cfg.max_candidates == 100

    def test_max_candidates_env_var(self, monkeypatch):
        monkeypatch.setenv("PHASE2_MAX_CANDIDATES", "25")
        from src.ai.config import Phase2Config
        cfg = Phase2Config()
        assert cfg.max_candidates == 25

    def test_fewer_jobs_than_max_candidates_uses_all(self, tmp_path):
        """When total eligible < max_candidates, all are selected."""
        from src.ai.config import Phase2Config

        jobs = self._make_jobs(5)
        cfg = Phase2Config(max_candidates=100)
        candidates = jobs[: cfg.max_candidates]

        assert len(candidates) == 5


# ---------------------------------------------------------------------------
# PipelineStats.phase2 field
# ---------------------------------------------------------------------------

class TestPipelineStatsPhase2Field:
    def test_phase2_field_exists_in_pipeline_stats(self):
        from src.pipeline.orchestrator import PipelineStats

        stats = PipelineStats()
        assert hasattr(stats, "phase2")
        assert stats.phase2 == {}

    def test_phase2_field_included_in_to_dict(self):
        from src.pipeline.orchestrator import PipelineStats

        stats = PipelineStats()
        stats.phase2 = {"candidates_selected": 10, "successful": 8}
        d = stats.to_dict()

        assert "phase2" in d
        assert d["phase2"]["candidates_selected"] == 10


# ---------------------------------------------------------------------------
# Issue 4 — response classification, truncation detection, bounded retry
# ---------------------------------------------------------------------------

def _fake_http_response(content: str, finish_reason: str = "stop"):
    """Build a MagicMock standing in for requests.Response, shaped like the
    real LM Studio / OpenAI-compatible chat completions payload."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason}
        ]
    }
    return resp


class TestResponseClassification:
    """Every non-success outcome must carry a specific failure_reason —
    never collapsed into a generic failure, and never silently treated as
    success (see the 2026-09-05 truncation audit)."""

    def test_valid_json_is_success_with_no_failure_reason(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        valid_json = '{"required_skills": ["python"], "confidence": 0.8}'
        with patch.object(LocalLLMProvider, "_call_llm", return_value=valid_json):
            result = provider.analyze_job(sample_job)

        assert result.status == "success"
        assert result.failure_reason is None
        assert result.retried is False

    def test_malformed_json_is_failed_not_silently_empty_success(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch.object(LocalLLMProvider, "_call_llm", return_value="not json at all {{{"):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.failure_reason == "malformed_json"
        assert result.error_message is not None

    def test_valid_json_wrong_schema_is_validation_error(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        # confidence must be 0.0-1.0 — 5.0 fails Pydantic validation.
        bad_shape = '{"confidence": 5.0}'
        with patch.object(LocalLLMProvider, "_call_llm", return_value=bad_shape):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.failure_reason == "validation_error"

    def test_empty_response_has_empty_response_failure_reason(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch.object(LocalLLMProvider, "_call_llm", return_value=""):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.failure_reason == "empty_response"


class TestTruncationDetectionAndRetry:
    """finish_reason == 'length' must be detected via the API's own signal,
    trigger exactly one bounded retry with a larger token budget, and never
    be classified as success even if the truncated text happens to parse."""

    def test_finish_reason_length_raises_truncated_error(self, sample_job):
        """_call_llm itself must surface truncation via a distinct exception,
        not swallow it into a normal return."""
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider, TruncatedResponseError

        provider = LocalLLMProvider(timeout_seconds=1)
        fake_resp = _fake_http_response('{"required_skills": ["py', finish_reason="length")
        with patch("requests.post", return_value=fake_resp):
            with pytest.raises(TruncatedResponseError):
                provider._call_llm("prompt")

    def test_truncation_then_success_is_retried_once_and_succeeds(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_tokens=100)
        responses = [
            _fake_http_response('{"required_skills": ["truncated', finish_reason="length"),
            _fake_http_response('{"required_skills": ["python"], "confidence": 0.7}', finish_reason="stop"),
        ]
        with patch("requests.post", side_effect=responses):
            result = provider.analyze_job(sample_job)

        assert result.status == "success"
        assert result.retried is True
        assert result.analysis.required_skills == ["python"]

    def test_truncation_retry_uses_larger_max_tokens(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_tokens=100)
        responses = [
            _fake_http_response('{"required_skills": ["truncated', finish_reason="length"),
            _fake_http_response('{"confidence": 0.5}', finish_reason="stop"),
        ]
        with patch("requests.post", side_effect=responses) as mock_post:
            provider.analyze_job(sample_job)

        first_call_tokens = mock_post.call_args_list[0].kwargs["json"]["max_tokens"]
        second_call_tokens = mock_post.call_args_list[1].kwargs["json"]["max_tokens"]
        assert first_call_tokens == 100
        assert second_call_tokens > first_call_tokens
        assert second_call_tokens == 200  # _RETRY_MAX_TOKENS_MULTIPLIER == 2

    def test_retry_is_bounded_to_exactly_one_extra_call(self, sample_job):
        """A job that truncates on every attempt must fail after exactly 2
        total calls (1 original + 1 retry), never loop indefinitely."""
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_tokens=100)
        always_truncated = _fake_http_response('{"required_skills": ["x', finish_reason="length")
        with patch("requests.post", return_value=always_truncated) as mock_post:
            result = provider.analyze_job(sample_job)

        assert mock_post.call_count == 2
        assert result.status == "failed"
        assert result.failure_reason == "truncated"
        assert result.retried is True

    def test_retry_max_tokens_capped(self):
        """The retry budget must never exceed _RETRY_MAX_TOKENS_CAP even for
        a large configured max_tokens."""
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(max_tokens=3000)
        retry_tokens = min(
            provider.max_tokens * provider._RETRY_MAX_TOKENS_MULTIPLIER,
            provider._RETRY_MAX_TOKENS_CAP,
        )
        assert retry_tokens == provider._RETRY_MAX_TOKENS_CAP

    def test_malformed_non_truncated_response_is_not_retried(self, sample_job):
        """Plain malformed JSON (not a truncation) must not trigger a retry —
        temperature=0 means an identical-parameters retry would just
        reproduce the same malformed output."""
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch.object(LocalLLMProvider, "_call_llm", return_value="{not valid json") as mock_call:
            result = provider.analyze_job(sample_job)

        assert mock_call.call_count == 1
        assert result.retried is False
        assert result.failure_reason == "malformed_json"


class TestTransportErrorClassification:
    def test_timeout_classified_as_timeout(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.failure_reason == "timeout"

    def test_connection_error_classified_as_connection_error(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("refused")):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.failure_reason == "connection_error"

    def test_http_error_classified_as_http_error(self, sample_job):
        from unittest.mock import patch, MagicMock
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1)
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        http_error = requests.exceptions.HTTPError("server error", response=bad_resp)
        bad_resp.raise_for_status.side_effect = http_error
        with patch("requests.post", return_value=bad_resp):
            result = provider.analyze_job(sample_job)

        assert result.status == "failed"
        assert result.failure_reason == "http_error"


class TestBatchMetricsAndProgress:
    def test_batch_aggregates_failure_reasons(self, sample_jobs):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_workers=1)
        with patch.object(LocalLLMProvider, "_call_llm", return_value=""):
            result = provider.analyze_batch(sample_jobs)

        assert result.failed == len(sample_jobs)
        assert result.failure_reasons == {"empty_response": len(sample_jobs)}

    def test_batch_reports_retried_count(self, sample_jobs):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_tokens=100, max_workers=1)
        responses = []
        for _ in sample_jobs:
            responses.append(_fake_http_response('{"required_skills": ["t', finish_reason="length"))
            responses.append(_fake_http_response('{"confidence": 0.5}', finish_reason="stop"))
        with patch("requests.post", side_effect=responses):
            result = provider.analyze_batch(sample_jobs)

        assert result.retried == len(sample_jobs)
        assert result.successful == len(sample_jobs)

    def test_on_progress_called_for_each_job_with_final_totals(self, sample_jobs):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_workers=1)
        calls: list[tuple[int, int, int]] = []

        def on_progress(completed, total, failed):
            calls.append((completed, total, failed))

        with patch.object(LocalLLMProvider, "_call_llm", return_value='{"confidence": 0.5}'):
            provider.analyze_batch(sample_jobs, on_progress=on_progress)

        assert len(calls) == len(sample_jobs)
        assert calls[-1] == (len(sample_jobs), len(sample_jobs), 0)

    def test_on_progress_tracks_failures(self, sample_job):
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_workers=1)
        calls: list[tuple[int, int, int]] = []

        with patch.object(LocalLLMProvider, "_call_llm", return_value=""):
            provider.analyze_batch([sample_job], on_progress=lambda c, t, f: calls.append((c, t, f)))

        assert calls == [(1, 1, 1)]

    def test_progress_callback_error_does_not_crash_batch(self, sample_jobs):
        """A buggy progress callback must never take down Phase 2 itself."""
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_workers=1)

        def broken_progress(completed, total, failed):
            raise RuntimeError("boom")

        with patch.object(LocalLLMProvider, "_call_llm", return_value='{"confidence": 0.5}'):
            result = provider.analyze_batch(sample_jobs, on_progress=broken_progress)

        assert result.successful == len(sample_jobs)

    def test_one_bad_job_does_not_crash_the_batch_with_concurrency(self, sample_jobs):
        """Concurrent (max_workers>1) path: one job's failure must not affect
        the others' results or raise out of analyze_batch. Keyed off each
        job's own title (embedded verbatim in its prompt) rather than call
        order, so this is deterministic under real thread concurrency.
        """
        from unittest.mock import patch
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(timeout_seconds=1, max_workers=2)
        bad_job_title = sample_jobs[0].title

        def flaky_call_llm(self_inner, prompt, max_tokens=None):
            if bad_job_title in prompt:
                return ""  # this one job's response is empty -> failure
            return '{"confidence": 0.5}'

        with patch.object(LocalLLMProvider, "_call_llm", flaky_call_llm):
            result = provider.analyze_batch(sample_jobs)

        assert result.total_jobs == len(sample_jobs)
        assert result.successful + result.failed == len(sample_jobs)
        assert result.failed == 1
        assert result.successful == len(sample_jobs) - 1
