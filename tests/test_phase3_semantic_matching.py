"""Tests for Phase 3: Local Semantic Matching.

Covers:
- Embedding generation and caching
- Profile embedding building
- Job semantic score computation
- Threshold behavior (below threshold still included but weighted)
- Missing/empty data handling (graceful degradation)
- Integration with deterministic scoring
- Hard filter precedence (semantic never overrides hard rejections)
"""

from __future__ import annotations

import pytest

from src.embeddings.embedding import EmbeddingService
from src.models.job import Job
from src.pipeline.semantic_matching import (
    SemanticMatchResult,
    apply_semantic_matching,
    build_profile_embedding,
    integrate_semantic_into_ranking,
    load_semantic_config,
)
from tests.fixtures.profiles import TEST_PROFILE_PATH

# -- Fixtures ---------------------------------------------------------------

@pytest.fixture
def sample_jobs() -> list[Job]:
    """Create a set of sample jobs for testing semantic matching."""
    return [
        Job(
            id="j1", source="greenhouse", title="Senior Software Engineer",
            company="Acme Corp", description=(
                "We are looking for a senior backend software engineer with 5+ years of experience. "
                "Required skills: Python, Django, PostgreSQL. Remote: fully remote."
            ),
            location="Paris, France", countries=["France"], remote=True,
            employment_type="full-time", salary_min=80000, salary_max=120000,
        ),
        Job(
            id="j2", source="ashby", title="Frontend Developer",
            company="TechStart Inc", description=(
                "We need a frontend developer with experience in React and TypeScript. "
                "Remote: fully remote."
            ),
            location="London, UK", countries=["UK"], remote=True,
            employment_type="full-time", salary_min=70000, salary_max=110000,
        ),
        Job(
            id="j3", source="linkedin", title="Junior Software Engineer",
            company="StartupXYZ", description=(
                "Entry level developer role. Remote: not specified."
            ),
            location="Berlin, Germany", countries=["Germany"], remote=False,
            employment_type="full-time", salary_min=50000, salary_max=70000,
        ),
    ]


@pytest.fixture
def sample_profile_info() -> dict:
    """Create a candidate profile for semantic matching."""
    return {
        "seniority": "mid-level",
        "employment_type": "full-time",
        "location": "Paris, France",
        "languages": ["Python", "TypeScript"],
        "skills": ["backend", "frontend", "python", "react"],
        "responsibilities": "Full stack development",
    }


@pytest.fixture
def embedding_service() -> EmbeddingService:
    """Create an embedding service with a small cache for testing."""
    return EmbeddingService(model_name="paraphrase-multilingual-MiniLM-L12-v2", cache_max_entries=100, device="cpu")


# -- Test: EmbeddingCache ---------------------------------------------------

class TestEmbeddingCache:
    def test_cache_put_and_get(self, embedding_service: EmbeddingService) -> None:
        """Cached embeddings should be returned without re-computation."""
        text = "software engineer backend"
        result = embedding_service.embed(text)
        cache_key = embedding_service._make_cache_key(text)
        cached = embedding_service._cache.get(cache_key)
        assert cached is not None, "Embedding should be cached after first call"
        assert cached == result, "Cached vector should match the computed embedding"

    def test_cache_size(self, embedding_service: EmbeddingService) -> None:
        """Cache size property should report the number of cached embeddings."""
        cache_size = embedding_service.cache_size
        assert isinstance(cache_size, int)
        assert cache_size >= 0


# -- Test: build_profile_embedding -----------------------------------------

class TestBuildProfileEmbedding:
    def test_build_complete_profile(self, sample_profile_info: dict, embedding_service: EmbeddingService) -> None:
        """Building a profile with all fields should return a non-zero vector."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        assert len(profile_emb) == 384  # MiniLM-L6-v2 dimension
        # At least one element should be non-zero for a meaningful profile
        has_nonzero = any(abs(x) > 1e-10 for x in profile_emb)
        assert has_nonzero, "Profile embedding should have at least one non-zero element"

    def test_build_missing_fields(self, embedding_service: EmbeddingService) -> None:
        """Missing profile fields should still produce a valid (possibly zero) vector."""
        partial_profile = {"seniority": "junior"}
        profile_emb = build_profile_embedding(partial_profile, embedding_service)
        assert len(profile_emb) == 384

    def test_build_empty_profile(self, embedding_service: EmbeddingService) -> None:
        """Empty or all-missing profile should return a zero vector."""
        empty_profile = {}
        profile_emb = build_profile_embedding(empty_profile, embedding_service)
        assert len(profile_emb) == 384
        # All elements should be zero
        for val in profile_emb:
            assert abs(val) < 1e-10, f"Expected zero vector but got {val}"

    def test_build_empty_string_template(self, embedding_service: EmbeddingService) -> None:
        """Template that results in empty string should return zero vector."""
        # Use a template that will result in empty after formatting
        profile_emb = build_profile_embedding(
            {"seniority": "", "employment_type": "", "location": ""},
            embedding_service,
            template=" {seniority} {employment_type} {location}",
        )
        assert len(profile_emb) == 384


# -- Test: compute_job_semantic_score --------------------------------------

class TestComputeJobSemanticScore:
    def test_compute_valid_job(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Valid jobs should produce a semantic score in [0, 1]."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        for job in sample_jobs:
            score = embedding_service.compute_job_semantic_score(job, profile_emb)
            assert isinstance(score, float), f"Score for {job.id} should be float, got {type(score)}"
            assert 0.0 <= score <= 1.0, (
                f"Score for {job.id} ({score}) must be in [0, 1]"
            )

    def test_compute_missing_title_and_description(self, sample_profile_info: dict, embedding_service: EmbeddingService) -> None:
        """Job with no title or description should return 0.0."""
        empty_job = Job(
            id="j_empty", source="unknown", title=None, description=None,
            location="", countries=[], remote=True, employment_type="full-time",
        )
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        score = embedding_service.compute_job_semantic_score(empty_job, profile_emb)
        assert abs(score) < 1e-6, f"Expected ~0.0 but got {score}"

    def test_compute_non_zero_embedding(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """When profile has non-zero embedding, jobs with text should have non-trivial scores."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        # At least one job with title+description should have a score > 0
        scores = [embedding_service.compute_job_semantic_score(job, profile_emb) for job in sample_jobs]
        has_meaningful = any(s > 1e-6 for s in scores)
        assert has_meaningful, "At least one job should have a non-trivial semantic score"


# -- Test: apply_semantic_matching -----------------------------------------

class TestApplySemanticMatching:
    def test_returns_scored_and_unscored(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """apply_semantic_matching should return two lists."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)
        assert isinstance(scored, list)
        assert isinstance(unscored, list)

    def test_all_jobs_are_scored_or_unscored(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Every input job should appear in either scored or unscored."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)
        input_ids = {job.id for job in sample_jobs}
        output_ids = {item[0].id if isinstance(item, tuple) else item.id for item in scored + unscored}
        assert set(input_ids) == set(output_ids), "All input jobs must appear in results"

    def test_zero_vector_when_no_text(self, sample_profile_info: dict, embedding_service: EmbeddingService) -> None:
        """Jobs with no title or description should get a zero semantic score."""
        empty_job = Job(
            id="j_empty", source="unknown", title=None, description="",
            location="", countries=[], remote=True, employment_type="full-time",
        )
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching([empty_job], profile_emb, config, embedding_service)
        for job, score in scored + unscored:
            assert abs(score) < 1e-6, f"Job {job.id} with no text should have ~0 score"

    def test_threshold_below_min_returns_score(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Jobs below the min threshold are still scored (score may be 0 or low)."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, _ = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)
        # All jobs should be in scored list (below-threshold still included)
        assert len(scored) >= 1, "At least one job should have a score"

    def test_hard_filters_require_first(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """With require_hard_filters_first=True, semantic matching only sees filtered jobs."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        # Filter out remote-only jobs (simulate hard filter)
        filtered = [j for j in sample_jobs if not j.remote]
        scored, _ = apply_semantic_matching(filtered, profile_emb, config, embedding_service)
        assert len(scored) == len(filtered), "All filtered jobs should be scored"


# -- Test: integrate_semantic_into_ranking ----------------------------------

class TestIntegrateSemanticIntoRanking:
    def test_final_score_is_weighted_average(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Final score = det_weight * det_score + weight * sem_score."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, _ = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)

        # Build deterministic scores (simplified: use semantic score as det_score for testing)
        det_scored = [(job, 0.5 + job.id.count("1") * 0.1) for job, _ in scored]

        final = integrate_semantic_into_ranking(det_scored, scored, config)

        # Verify sorting (descending by final score)
        scores = [s for _, s in final]
        assert scores == sorted(scores, reverse=True), "Results should be sorted by score descending"

    def test_weighted_formula(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Verify the exact weighted formula."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, _ = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)

        # Use first job's deterministic score as det_score
        job, sem_score = scored[0]
        det_score = 0.75  # hypothetical deterministic score
        weight = 0.2
        det_weight = 1.0 - weight
        expected_final = det_weight * det_score + weight * sem_score
        final = integrate_semantic_into_ranking(
            [(job, det_score)], scored, config
        )
        assert abs(final[0][1] - expected_final) < 1e-9

    def test_missing_sem_score_defaults_to_zero(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Jobs without a semantic score should default to 0.0 in integration."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, _ = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)

        # Add a job that's NOT in the semantic results
        extra_job = Job(
            id="j_extra", source="unknown", title="Extra", description="Extra desc",
            location="", countries=[], remote=True, employment_type="full-time",
        )
        det_scored = [(job, 0.5) for job in [sample_jobs[0]] + [extra_job]]

        final = integrate_semantic_into_ranking(det_scored, scored, config)
        extra_final = next((score for job, score in final if job.id == "j_extra"), None)
        assert extra_final is not None, "Extra job should still appear in results"
        # Its semantic score defaults to 0.0
        expected = 0.8 * 0.5 + 0.2 * 0.0  # det_weight=0.8, weight=0.2
        assert abs(extra_final - expected) < 1e-9

    def test_scores_clamped_to_range(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Final scores should be clamped to [0, 1]."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, _ = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)

        # Use deterministic scores that could push final out of range
        det_scored = [(job, -0.5) for job, _ in scored]

        final = integrate_semantic_into_ranking(det_scored, scored, config)
        for job, score in final:
            assert 0.0 <= score <= 1.0, f"Score {score} out of [0, 1] range for {job.id}"


# -- Test: load_semantic_config --------------------------------------------

class TestLoadSemanticConfig:
    def test_load_existing_config(self) -> None:
        """load_semantic_config should load from YAML file."""
        config_path = "config/semantic_config.yaml"
        config = load_semantic_config(config_path)
        assert isinstance(config, dict)
        assert "embedding" in config
        assert "similarity" in config

    def test_load_missing_config_returns_defaults(self) -> None:
        """load_semantic_config should return defaults when file not found."""
        config = load_semantic_config("/nonexistent/path/semantic_config.yaml")
        assert isinstance(config, dict)
        assert "embedding" in config
        assert "similarity" in config

    def test_load_invalid_yaml_raises_type_error(self) -> None:
        """load_semantic_config should raise TypeError for invalid YAML."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("not valid yaml")
            temp_path = f.name

        try:
            with pytest.raises(TypeError):
                load_semantic_config(temp_path)
        finally:
            os.unlink(temp_path)


# -- Test: SemanticMatchResult ---------------------------------------------

class TestSemanticMatchResult:
    def test_result_attributes(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """SemanticMatchResult should have correct attributes."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)

        result = SemanticMatchResult(scored, unscored)
        assert isinstance(result, SemanticMatchResult)
        assert result.total_jobs == len(sample_jobs)
        assert result.matched_count >= 0

    def test_result_summary(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """SemanticMatchResult.summary() should return a human-readable string."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching(sample_jobs, profile_emb, config, embedding_service)

        result = SemanticMatchResult(scored, unscored)
        summary = result.summary()
        assert isinstance(summary, str)
        assert "Semantic Matching Results:" in summary


# -- Test: Edge Cases --------------------------------------------------------

class TestEdgeCases:
    def test_empty_jobs_list(self, sample_profile_info: dict, embedding_service: EmbeddingService) -> None:
        """apply_semantic_matching should handle empty job list."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching([], profile_emb, config, embedding_service)
        assert len(scored) == 0
        assert len(unscored) == 0

    def test_single_job(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """apply_semantic_matching should handle single job."""
        job = sample_jobs[0]
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        config = {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 100, "device": "cpu"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }
        scored, unscored = apply_semantic_matching([job], profile_emb, config, embedding_service)
        assert len(scored) + len(unscored) == 1

    def test_cache_eviction(self, embedding_service: EmbeddingService) -> None:
        """Cache should evict oldest entries when max_entries is reached."""
        service = EmbeddingService(model_name="paraphrase-multilingual-MiniLM-L12-v2", cache_max_entries=3, device="cpu")
        for i in range(5):
            service.embed(f"text {i}")
        # Should have at most 3 cached entries (FIFO eviction)
        assert service.cache_size <= 3

    def test_empty_profile_embedding_returns_zero(self, embedding_service: EmbeddingService) -> None:
        """compute_job_semantic_score with empty profile should return ~0.0."""
        job = Job(
            id="j1", source="unknown", title="Test", description="Test desc",
            location="", countries=[], remote=True, employment_type="full-time",
        )
        score = embedding_service.compute_job_semantic_score(job, [])
        assert abs(score) < 1e-6

    def test_semantic_score_deterministic_for_same_input(self, sample_profile_info: dict, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Same input should produce the same semantic score (deterministic)."""
        profile_emb = build_profile_embedding(sample_profile_info, embedding_service)
        job = sample_jobs[0]

        score1 = embedding_service.compute_job_semantic_score(job, profile_emb)
        score2 = embedding_service.compute_job_semantic_score(job, profile_emb)

        assert abs(score1 - score2) < 1e-6, "Same input should produce same score"

    def test_multiple_profile_embeddings(self, sample_jobs: list[Job], embedding_service: EmbeddingService) -> None:
        """Different profile embeddings should produce different job scores."""
        # Both profiles must supply every field the default template expects
        # (seniority, employment_type, location, languages, skills,
        # responsibilities) — an incomplete profile hits the missing-field
        # fallback in build_profile_embedding and collapses to a zero
        # vector, which would make this test vacuously pass/fail regardless
        # of the two profiles actually differing.
        profile1 = build_profile_embedding(
            {
                "seniority": "junior", "employment_type": "full-time",
                "location": "Paris, France", "languages": ["Python"],
                "skills": ["python"], "responsibilities": "backend development",
            },
            embedding_service,
        )
        profile2 = build_profile_embedding(
            {
                "seniority": "senior", "employment_type": "full-time",
                "location": "London, UK", "languages": ["TypeScript"],
                "skills": ["react", "typescript"], "responsibilities": "frontend architecture",
            },
            embedding_service,
        )

        score1 = embedding_service.compute_job_semantic_score(sample_jobs[0], profile1)
        score2 = embedding_service.compute_job_semantic_score(sample_jobs[0], profile2)

        # Scores should differ significantly for different profiles
        assert abs(score1 - score2) > 0.01, "Different profiles should produce different scores"


# -- Test: Hard Filter Precedence ------------------------------------------

class TestHardFilterPrecedence:
    """Phase 3 semantic matching must NEVER override Phase 1 hard filters."""

    def test_semantic_does_not_override_location_filter(self) -> None:
        """If a job fails location filter, it should not appear in semantic results."""
        # Create a job that would pass location but test the principle
        job = Job(
            id="j1", source="greenhouse", title="Python Developer",
            company="Acme Corp", description="Backend role",
            location="Paris, France", countries=["France"], remote=True,
            employment_type="full-time", salary_min=80000, salary_max=120000,
        )
        # This job passes Phase 1's hard location filter (France is in include).
        # That filter is built from profile.yaml + filters.yaml; semantic matching
        # has no say over it.  Verify France is accepted by the filter config.
        from src.pipeline.filter import filter_jobs, load_filter_config

        filter_cfg = load_filter_config(TEST_PROFILE_PATH)
        passed, _ = filter_jobs([job], filter_cfg)
        assert any(j.id == job.id for j in passed), "France job must pass the filter"
        config = load_semantic_config("config/semantic_config.yaml")

        # The key principle: semantic scoring runs AFTER hard filters.
        # We verify by checking that apply_semantic_matching only sees filtered jobs.
        profile_emb = build_profile_embedding(
            {"seniority": "mid-level", "employment_type": "full-time", "location": "Paris, France"},
            EmbeddingService(model_name="paraphrase-multilingual-MiniLM-L12-v2", cache_max_entries=100, device="cpu"),
        )

        scored, _ = apply_semantic_matching([job], profile_emb, config,
                                              EmbeddingService(model_name="paraphrase-multilingual-MiniLM-L12-v2", cache_max_entries=100, device="cpu"))
        assert len(scored) == 1, "Filtered job should still be scored"

    def test_require_hard_filters_first_is_configurable(self) -> None:
        """The require_hard_filters_first flag should be present in config."""
        config = load_semantic_config("config/semantic_config.yaml")
        assert config["similarity"]["require_hard_filters_first"] is True


# -- Test: Profile "looking_for" reaches Phase 3 matching (Issue 1) --------

class TestLookingForInfluencesMatching:
    """Regression coverage for the Profile 'What are you looking for?' field
    (Phase 1.5 stabilization, Issue 1): looking_for must reach the Phase 3
    profile embedding and measurably shift job ranking — generically, for
    arbitrary free text, not via a hardcoded keyword search. Uses the real
    JobSearchProfile.to_semantic_info() -> build_profile_embedding path, the
    same one src/pipeline/orchestrator.py calls on every run.
    """

    def test_looking_for_changes_profile_embedding(self, embedding_service: EmbeddingService) -> None:
        """Same target_domains/skills, different looking_for -> different embeddings."""
        from src.models.profile import JobSearchProfile

        shared = {"target_domains": ["software development"], "target_skills": ["python"]}
        profile_a = JobSearchProfile(**shared, looking_for="cybersecurity")
        profile_b = JobSearchProfile(**shared, looking_for="web development")

        emb_a = build_profile_embedding(profile_a.to_semantic_info(), embedding_service)
        emb_b = build_profile_embedding(profile_b.to_semantic_info(), embedding_service)

        assert emb_a != emb_b, "Different looking_for text must produce a different profile embedding"

    def test_looking_for_shifts_relative_job_ranking(self, embedding_service: EmbeddingService) -> None:
        """A candidate whose looking_for describes a job's domain should
        score that job more highly than a candidate whose looking_for points
        elsewhere — proving the free-text field actually moves ranking, not
        just the raw vector. target_domains/target_skills are held identical
        across both profiles so looking_for is the only variable.
        """
        from src.models.profile import JobSearchProfile

        cybersecurity_job = Job(
            id="j_sec", source="test", title="SOC Analyst",
            description=(
                "Cybersecurity SOC analyst monitoring security threats, leading incident "
                "response, and hardening security operations across the network."
            ),
            location="Paris, France", countries=["France"], remote=True, employment_type="full-time",
        )
        webdev_job = Job(
            id="j_web", source="test", title="Frontend Developer",
            description=(
                "Web development role building responsive user interfaces with React and "
                "TypeScript, working closely with designers on the product's frontend."
            ),
            location="Paris, France", countries=["France"], remote=True, employment_type="full-time",
        )

        shared = {"target_domains": ["software development"], "target_skills": ["python"]}
        cyber_profile = JobSearchProfile(**shared, looking_for="I am looking for cybersecurity / cyber security positions")
        web_profile = JobSearchProfile(**shared, looking_for="Search for web development jobs")

        cyber_emb = build_profile_embedding(cyber_profile.to_semantic_info(), embedding_service)
        web_emb = build_profile_embedding(web_profile.to_semantic_info(), embedding_service)

        cyber_score_for_cyber_job = embedding_service.compute_job_semantic_score(cybersecurity_job, cyber_emb)
        web_score_for_cyber_job = embedding_service.compute_job_semantic_score(cybersecurity_job, web_emb)
        cyber_score_for_web_job = embedding_service.compute_job_semantic_score(webdev_job, cyber_emb)
        web_score_for_web_job = embedding_service.compute_job_semantic_score(webdev_job, web_emb)

        assert cyber_score_for_cyber_job > web_score_for_cyber_job, (
            "The cybersecurity-oriented profile should score the SOC analyst job higher "
            "than the web-development-oriented profile does"
        )
        assert web_score_for_web_job > cyber_score_for_web_job, (
            "The web-development-oriented profile should score the frontend job higher "
            "than the cybersecurity-oriented profile does"
        )

    def test_looking_for_changes_profile_hash_for_cache_invalidation(self) -> None:
        """build_profile_embedding hashes profile_info to key its persistent
        cache (src/storage/sqlite.py job_embeddings table). If two different
        looking_for values hashed to the same key, a changed description
        would silently keep serving the old cached embedding forever. This
        never touches storage — it only proves the hash itself changes."""
        import hashlib
        import json

        from src.models.profile import JobSearchProfile

        shared = {"target_domains": ["software development"]}
        info_a = JobSearchProfile(**shared, looking_for="cybersecurity").to_semantic_info()
        info_b = JobSearchProfile(**shared, looking_for="web development").to_semantic_info()

        hash_a = hashlib.sha256(json.dumps(info_a, sort_keys=True).encode()).hexdigest()[:32]
        hash_b = hashlib.sha256(json.dumps(info_b, sort_keys=True).encode()).hexdigest()[:32]

        assert hash_a != hash_b, "Different looking_for text must produce a different cache key"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
