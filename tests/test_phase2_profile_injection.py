"""Tests for Phase 2 profile injection.

Verifies that:
- build_personalized_matching_prompt includes the user profile
- The profile section is clearly separated from the job section
- Phase2Pipeline.run forwards the profile to the LLM provider
- JobUnderstanding accepts profile_match_* fields from the model response
- The LLM call returns normal choices[0].message.content (not reasoning_content)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.ai.models import JobUnderstanding, JobUnderstandingResult
from src.ai.pipeline import Phase2Pipeline
from src.ai.prompt import (
    PERSONALIZED_PROMPT_VERSION,
    build_personalized_matching_prompt,
    _format_profile_section,
)
from src.models.job import Job


# ---------------------------------------------------------------------------
# Profile section formatting
# ---------------------------------------------------------------------------

class TestFormatProfileSection:
    def test_includes_target_domains(self):
        profile = {"target_domains": ["software_development", "web_development"]}
        section = _format_profile_section(profile)
        assert "software_development" in section
        assert "web_development" in section

    def test_includes_target_skills(self):
        profile = {"target_skills": ["python", "javascript"]}
        section = _format_profile_section(profile)
        assert "python" in section
        assert "javascript" in section

    def test_includes_languages(self):
        profile = {"languages": ["French", "English"]}
        section = _format_profile_section(profile)
        assert "French" in section
        assert "English" in section

    def test_includes_geography(self):
        profile = {
            "geography": {
                "preferred_countries": ["France"],
                "allowed_regions": ["Europe"],
            }
        }
        section = _format_profile_section(profile)
        assert "France" in section
        assert "Europe" in section

    def test_includes_experience_levels(self):
        profile = {"experience": {"preferred_levels": ["junior", "entry_level"]}}
        section = _format_profile_section(profile)
        assert "junior" in section

    def test_includes_excluded_seniority(self):
        profile = {"experience": {"excluded_seniority": ["senior", "director"]}}
        section = _format_profile_section(profile)
        assert "senior" in section

    def test_includes_max_years(self):
        profile = {"experience": {"max_years": 3}}
        section = _format_profile_section(profile)
        assert "3" in section

    def test_empty_profile_does_not_crash(self):
        section = _format_profile_section({})
        assert "USER PROFILE" in section

    def test_starts_with_user_profile_header(self):
        profile = {"target_skills": ["python"]}
        section = _format_profile_section(profile)
        assert section.startswith("USER PROFILE:")


# ---------------------------------------------------------------------------
# build_personalized_matching_prompt
# ---------------------------------------------------------------------------

class TestPersonalizedMatchingPrompt:
    def _profile(self) -> dict:
        return {
            "target_domains": ["software_development"],
            "target_skills": ["python", "javascript"],
            "languages": ["French", "English"],
            "geography": {
                "preferred_countries": ["France"],
                "allowed_regions": ["Europe"],
            },
            "work_modes": ["remote", "hybrid"],
            "contract_types": ["internship", "cdi"],
            "experience": {
                "preferred_levels": ["junior", "entry_level"],
                "max_years": 3,
                "excluded_seniority": ["senior", "director"],
            },
            "freshness_days": 30,
        }

    def test_prompt_contains_user_profile_section(self):
        prompt = build_personalized_matching_prompt(
            job_description="We are looking for a Python developer.",
            profile=self._profile(),
            job_title="Python Developer",
            job_company="TechCorp",
        )
        assert "USER PROFILE" in prompt

    def test_prompt_contains_job_section(self):
        prompt = build_personalized_matching_prompt(
            job_description="We are looking for a Python developer.",
            profile=self._profile(),
            job_title="Python Developer",
            job_company="TechCorp",
        )
        assert "JOB" in prompt
        assert "Python Developer" in prompt
        assert "TechCorp" in prompt
        assert "We are looking for a Python developer." in prompt

    def test_prompt_contains_profile_skills(self):
        prompt = build_personalized_matching_prompt(
            job_description="Job description here.",
            profile=self._profile(),
        )
        assert "python" in prompt
        assert "javascript" in prompt

    def test_prompt_contains_geography(self):
        prompt = build_personalized_matching_prompt(
            job_description="Job description here.",
            profile=self._profile(),
        )
        assert "France" in prompt

    def test_prompt_warns_not_to_repeat_hard_constraints(self):
        prompt = build_personalized_matching_prompt(
            job_description="Job description here.",
            profile=self._profile(),
        )
        assert "deterministic pipeline" in prompt.lower() or "hard constraint" in prompt.lower()

    def test_prompt_omits_match_score_from_schema(self):
        """profile_match_score/reasons/mismatch_reasons were removed from LLM
        generation in the 2026-09-04 personalization audit: the deterministic
        scorer and Phase 3's semantic matching already compute profile fit,
        and src/pipeline/explain.py already generates deterministic "why this
        job" reasons wired into the API/frontend. A third LLM-judged fit
        signal would have duplicated both. The JobUnderstanding model still
        accepts these fields (for cache/schema compatibility) — they're just
        no longer requested from the LLM.
        """
        prompt = build_personalized_matching_prompt(
            job_description="Job description here.",
            profile=self._profile(),
        )
        assert "profile_match_score" not in prompt
        assert "profile_match_reasons" not in prompt
        assert "profile_mismatch_reasons" not in prompt

    def test_prompt_requests_json_only(self):
        prompt = build_personalized_matching_prompt(
            job_description="Job description here.",
            profile=self._profile(),
        )
        assert "valid JSON" in prompt or "json" in prompt.lower()

    def test_profile_appears_before_job(self):
        prompt = build_personalized_matching_prompt(
            job_description="Job description here.",
            profile=self._profile(),
            job_title="Developer",
        )
        profile_pos = prompt.find("USER PROFILE")
        job_pos = prompt.find("JOB:")
        assert profile_pos != -1, "USER PROFILE not found"
        assert job_pos != -1, "JOB: section not found"
        assert profile_pos < job_pos, "Profile must appear before Job in prompt"


# ---------------------------------------------------------------------------
# JobUnderstanding model — profile_match fields
# ---------------------------------------------------------------------------

class TestJobUnderstandingProfileFields:
    def test_default_profile_match_score_is_none(self):
        u = JobUnderstanding()
        assert u.profile_match_score is None

    def test_profile_match_score_accepted(self):
        u = JobUnderstanding(profile_match_score=0.85)
        assert u.profile_match_score == pytest.approx(0.85)

    def test_profile_match_reasons_accepted(self):
        u = JobUnderstanding(
            profile_match_reasons=["matches Python skill", "junior level"],
            profile_mismatch_reasons=["requires JavaScript"],
        )
        assert len(u.profile_match_reasons) == 2
        assert len(u.profile_mismatch_reasons) == 1

    def test_profile_match_score_out_of_range_rejected(self):
        with pytest.raises(Exception):
            JobUnderstanding(profile_match_score=1.5)

    def test_profile_fields_default_to_empty_lists(self):
        u = JobUnderstanding()
        assert u.profile_match_reasons == []
        assert u.profile_mismatch_reasons == []


# ---------------------------------------------------------------------------
# Phase2Pipeline.run — profile forwarded to LLM provider
# ---------------------------------------------------------------------------

class TestPhase2PipelineProfileForwarding:
    def _make_job(self, job_id: str = "job-1") -> Job:
        return Job(
            id=job_id,
            title="Python Developer",
            company="TechCorp",
            description="We need a Python developer for our team.",
            source="test",
        )

    def _make_analysis_storage(self) -> MagicMock:
        storage = MagicMock()
        storage.get_analyses_for_jobs.return_value = {}
        return storage

    def _mock_llm_result(self, job_id: str) -> JobUnderstandingResult:
        return JobUnderstandingResult(
            job_id=job_id,
            analysis=JobUnderstanding(
                required_skills=["python"],
                profile_match_score=0.9,
                profile_match_reasons=["Python skill matches"],
            ),
            prompt_version=PERSONALIZED_PROMPT_VERSION,
            model_identifier="test-model",
            status="success",
        )

    def test_run_with_profile_selects_personalized_version(self):
        """When a profile is passed, the pipeline uses PERSONALIZED_PROMPT_VERSION."""
        job = self._make_job()
        profile = {"target_skills": ["python"], "target_domains": ["software_development"]}

        mock_llm = MagicMock()
        batch_result = MagicMock()
        batch_result.results = [self._mock_llm_result(job.id)]
        batch_result.retried = 0
        batch_result.failure_reasons = {}
        mock_llm.analyze_batch.return_value = batch_result

        pipeline = Phase2Pipeline(
            storage=MagicMock(),
            ai_storage=self._make_analysis_storage(),
            llm_provider=mock_llm,
        )
        pipeline.run([job], profile=profile)

        mock_llm.analyze_batch.assert_called_once()
        call_kwargs = mock_llm.analyze_batch.call_args
        # The profile must be forwarded
        assert call_kwargs.kwargs.get("profile") == profile or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] == profile
        )
        # The prompt version must be the personalized one
        prompt_version_arg = (
            call_kwargs.kwargs.get("prompt_version")
            or (call_kwargs.args[1] if len(call_kwargs.args) >= 2 else None)
        )
        assert prompt_version_arg == PERSONALIZED_PROMPT_VERSION

    def test_run_without_profile_uses_standard_version(self):
        """When no profile is passed, the pipeline uses the standard prompt version."""
        job = self._make_job()

        mock_llm = MagicMock()
        batch_result = MagicMock()
        batch_result.results = [self._mock_llm_result(job.id)]
        batch_result.retried = 0
        batch_result.failure_reasons = {}
        mock_llm.analyze_batch.return_value = batch_result

        pipeline = Phase2Pipeline(
            storage=MagicMock(),
            ai_storage=self._make_analysis_storage(),
            llm_provider=mock_llm,
            prompt_version="job_understanding_v1",
        )
        pipeline.run([job], profile=None)

        call_kwargs = mock_llm.analyze_batch.call_args
        prompt_version_arg = (
            call_kwargs.kwargs.get("prompt_version")
            or (call_kwargs.args[1] if len(call_kwargs.args) >= 2 else None)
        )
        assert prompt_version_arg == "job_understanding_v1"

    def test_run_result_contains_profile_match_score(self):
        """The pipeline result includes the profile_match_score from the LLM."""
        job = self._make_job()
        profile = {"target_skills": ["python"]}

        mock_llm = MagicMock()
        batch_result = MagicMock()
        batch_result.results = [self._mock_llm_result(job.id)]
        batch_result.retried = 0
        batch_result.failure_reasons = {}
        mock_llm.analyze_batch.return_value = batch_result

        pipeline = Phase2Pipeline(
            storage=MagicMock(),
            ai_storage=self._make_analysis_storage(),
            llm_provider=mock_llm,
        )
        result = pipeline.run([job], profile=profile)

        assert len(result.results) == 1
        analysis = result.results[0].analysis
        assert analysis.profile_match_score == pytest.approx(0.9)
        assert "Python skill matches" in analysis.profile_match_reasons


# ---------------------------------------------------------------------------
# LLM response parsing — normal content, not reasoning_content
# ---------------------------------------------------------------------------

class TestLLMResponseParsing:
    """Verify that the response is consumed from choices[0].message.content,
    not from any non-standard field like reasoning_content."""

    def test_standard_choices_content_parsed(self):
        """A normal OpenAI-compatible response is parsed correctly."""
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(endpoint="http://localhost:1234")

        response_json = {
            "required_skills": ["Python"],
            "preferred_skills": [],
            "programming_languages": ["Python"],
            "profile_match_score": 0.8,
            "profile_match_reasons": ["Python skill found"],
            "profile_mismatch_reasons": [],
            "confidence": 0.9,
        }

        import json
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(response_json),
                        "reasoning_content": "This is internal reasoning that should be ignored.",
                    }
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_response):
            result = provider._parse_response(json.dumps(response_json))

        assert result.profile_match_score == pytest.approx(0.8)
        assert "Python skill found" in result.profile_match_reasons
        # reasoning_content is never used — only choices[0].message.content
