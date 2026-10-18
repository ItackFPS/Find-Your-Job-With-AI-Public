"""Integration tests for the requirements-gate LLM upgrade (rejection safety).

Covers the complete path the orchestrator actually drives:

    profile.matching → build_requirements_llm_provider → RequirementExtractor
        → LLMProvider.complete → _parse_llm_response (corroboration)
        → _merge (precedence) → HardConstraintGate → GateResult

All LLM calls are mocked at the public ``LLMProvider.complete`` boundary (or,
for a real ``LocalLLMProvider`` instance, at its underlying ``_call_llm`` —
both boundaries are exercised below) — or, for the "unavailable" case, driven
against a real closed port — so these tests
stay deterministic, offline, free, and fast. No mock ever stands in for
``RequirementExtractor`` or ``HardConstraintGate`` themselves — those run for
real in every test, which is what makes this "orchestrator-level": it proves
the actual wiring function orchestrator.py calls, not a re-implementation of it.

See also:
  - tests/test_hard_filter_integration.py::TestAIBoundary — the original,
    narrower "LLM can't set reject:true" tests this suite complements.
  - src/requirements/extractor.py module docstring — the evidence/
    corroboration design these tests verify.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.ai.interfaces import LocalLLMProvider
from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.requirements.extractor import (
    RequirementExtractor,
    build_requirements_llm_provider,
    should_run_llm,
)
from src.requirements.gate import HardConstraintGate
from src.requirements.models import EvidenceSource, Importance


def _job(
    *,
    job_id: str = "j1",
    title: str = "Software Engineer",
    description: str = "",
) -> Job:
    return Job(
        id=job_id,
        source="test",
        title=title,
        company="Acme",
        description=description,
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        discovered_at=datetime.now(UTC),
    )


def _llm_json(**kwargs) -> str:
    payload = {
        "education": None,
        "required_skills": [],
        "preferred_skills": [],
        "is_principal_tech_role": False,
        "principal_technologies": [],
    }
    payload.update(kwargs)
    return json.dumps(payload)


# ============================================================================
# Test 1 — LLM actually runs when enabled
# ============================================================================

class TestLLMRunsWhenEnabled:
    def test_provider_constructed_when_flag_enabled(self):
        matching = JobSearchProfile(matching={"llm_extraction_enabled": True}).matching
        provider = build_requirements_llm_provider(matching)
        assert isinstance(provider, LocalLLMProvider)

    def test_call_llm_actually_invoked_during_extraction(self):
        matching = JobSearchProfile(matching={"llm_extraction_enabled": True}).matching
        provider = build_requirements_llm_provider(matching)
        assert provider is not None

        with patch.object(
            LocalLLMProvider, "_call_llm", return_value=_llm_json()
        ) as mock_call:
            extractor = RequirementExtractor(user_skills=["python"], llm_provider=provider)
            job = _job(description="Python developer wanted. Django experience appreciated.")
            result = extractor.extract(job)

        mock_call.assert_called_once()
        assert result.extraction_method == "combined"


# ============================================================================
# Test 2 — LLM disabled
# ============================================================================

class TestLLMDisabled:
    def test_provider_is_none_when_flag_disabled(self):
        matching = JobSearchProfile(matching={"llm_extraction_enabled": False}).matching
        assert build_requirements_llm_provider(matching) is None

    def test_no_llm_call_and_heuristic_only_result(self):
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=None)
        job = _job(description="Bac+5 requis. Python developer.")
        result = extractor.extract(job)
        assert result.extraction_method == "heuristic"


# ============================================================================
# Test 3 — Useful LLM interpretation reaches the gate
# ============================================================================

class TestUsefulLLMInterpretation:
    def test_llm_catches_principal_tech_the_heuristic_misses(self):
        """Generic title + indirect prose mentioning Rust prominently.

        The heuristic's principal-skill detector only looks at the TITLE, so
        a generic title like "Backend Engineer" never flags Rust even when
        the description makes it clearly central. The LLM can read that
        prose — and because Rust genuinely appears in the text, the claim is
        corroborated and allowed to reach the gate's evidence.
        """
        description = (
            "You will work extensively with Rust and should already be "
            "comfortable developing production Rust services."
        )
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Backend Engineer", description=description)

        heuristic_only = RequirementExtractor(user_skills=["python"]).extract(job)
        assert "rust" not in heuristic_only.principal_skill_mismatches, (
            "test setup invalid: heuristic must NOT already catch this from the title"
        )

        result = extractor.extract(job)
        assert "rust" in result.principal_skill_mismatches
        assert "rust" not in result.principal_skill_uncorroborated


# ============================================================================
# Test 4 — Hallucinated education must NOT reject
# ============================================================================

class TestHallucinatedEducation:
    def test_unsupported_bac5_claim_does_not_reject(self):
        description = "Join our backend team. Great environment, competitive salary."
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            education={"level": "bac+5", "importance": "mandatory"},
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Backend Developer", description=description)

        req = extractor.extract(job)
        assert req.education is not None
        assert req.education.source == EvidenceSource.LLM_UNCORROBORATED

        result = HardConstraintGate(user_max_education_level=2).check(req)
        assert result.passes, "Uncorroborated LLM education claim must not cause rejection"
        assert any("discarded as unsupported" in f for f in result.flags)

    def test_unsupported_bac5_claim_does_not_apply_soft_penalty(self):
        from src.requirements.gate import gate_jobs

        description = "Join our backend team. Great environment, competitive salary."
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            education={"level": "bac+5", "importance": "preferred"},
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Backend Developer", description=description)
        req = extractor.extract(job)

        gate = HardConstraintGate(user_max_education_level=2)
        _, results = gate_jobs([job], {job.id: req}, gate)
        assert results[job.id].passes
        assert results[job.id].education_penalty == 0.0


# ============================================================================
# Test 5 — Hallucinated technology must NOT reject
# ============================================================================

class TestHallucinatedTechnology:
    def test_unsupported_rust_claim_does_not_reject(self):
        description = "Join our backend team. Great environment, competitive salary."
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Backend Developer", description=description)

        req = extractor.extract(job)
        assert "rust" not in req.principal_skill_mismatches
        assert "rust" in req.principal_skill_uncorroborated

        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert result.passes, "Uncorroborated LLM tech claim must not cause rejection"
        assert any("rust" in f.lower() and "discarded" in f for f in result.flags)


# ============================================================================
# Test 6 — Corroborated technology DOES produce the appropriate hard-filter behavior
# ============================================================================

class TestCorroboratedTechnology:
    def test_genuinely_present_rust_requirement_is_rejected(self):
        description = (
            "You will work extensively with Rust and should already be "
            "comfortable developing production Rust services."
        )
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Backend Engineer", description=description)

        req = extractor.extract(job)
        result = HardConstraintGate(user_max_education_level=5).check(req)

        assert not result.passes
        assert any(r.type == "principal_skill" and r.requirement == "rust" for r in result.reasons)


# ============================================================================
# Test 7 — Deterministic evidence beats a contradicting/weaker LLM claim
# ============================================================================

class TestDeterministicEvidenceWins:
    def test_heuristic_mandatory_bac5_survives_llm_disagreement(self):
        description = "Bac+5 obligatoire. Python developer role."
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            education={"level": "bac+5", "importance": "preferred"},
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description=description)

        req = extractor.extract(job)
        # Heuristic already found MANDATORY — merge must not let LLM soften it.
        assert req.education.importance.value == "mandatory"
        assert req.education.source == EvidenceSource.HEURISTIC

        result = HardConstraintGate(user_max_education_level=2).check(req)
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)

    def test_heuristic_mandatory_bac5_survives_llm_unknown(self):
        description = "Bac+5 obligatoire. Python developer role."
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(education=None)
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description=description)

        req = extractor.extract(job)
        result = HardConstraintGate(user_max_education_level=2).check(req)
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)


# ============================================================================
# Test 8 — Malformed LLM output falls back gracefully
# ============================================================================

class TestMalformedOutput:
    def test_garbage_json_falls_back_to_heuristic_result(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = "THIS IS NOT JSON {{{ broken"
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description="Bac+3 requis minimum. Python developer.")

        req = extractor.extract(job)
        assert req.extraction_method == "heuristic"
        assert req.education is not None
        assert req.education.level_bac_plus == 3


# ============================================================================
# Test 9 — LLM unavailable (real network failure, not mocked JSON)
# ============================================================================

class TestLLMUnavailable:
    def test_real_connection_failure_falls_back_safely(self):
        """Uses the real (unmocked) LocalLLMProvider against a closed port.

        Deterministic and fast: connecting to a closed local TCP port fails
        immediately, no real network flakiness involved.

        Job text is deliberately chosen so the heuristic finds no principal-
        skill mismatch and no mandatory education (generic engineering role,
        no core-language title token) — so ``should_run_llm`` genuinely
        returns True and the call is actually attempted, exercising the real
        connection-failure path rather than being skipped by selectivity.
        """
        provider = LocalLLMProvider(
            endpoint="http://localhost:1", model_name="local-model", timeout_seconds=2,
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=provider)
        job = _job(
            title="Backend Engineer",
            description="Join our engineering team building modern backend services. A relevant university degree is a plus.",
        )
        assert should_run_llm(job, RequirementExtractor(user_skills=["python"])._extract_heuristic(job)), (
            "test setup invalid: this job must trigger should_run_llm so the connection failure path is exercised"
        )

        result = extractor.extract(job)  # must not raise
        assert result.extraction_method == "heuristic"


# ============================================================================
# Test 10 — LLM disabled in normal (default) configuration
# ============================================================================

class TestDefaultConfigurationUnchanged:
    def test_default_profile_has_llm_disabled(self):
        profile = JobSearchProfile()
        assert profile.matching.llm_extraction_enabled is False

    def test_default_profile_gate_behavior_is_heuristic_only(self):
        profile = JobSearchProfile(max_education="bac+2", target_skills=["python"])
        provider = build_requirements_llm_provider(profile.matching)
        assert provider is None

        extractor = RequirementExtractor(user_skills=list(profile.target_skills), llm_provider=provider)
        job = _job(description="Bac+5 requis. Java obligatoire.", title="Java Developer")
        req = extractor.extract(job)
        assert req.extraction_method == "heuristic"

        result = HardConstraintGate(user_max_education_level=2).check(req)
        assert not result.passes
        assert {r.type for r in result.reasons} == {"education", "principal_skill"}


# ============================================================================
# Fix 1 — JS-family expansion reused on the LLM principal-tech/skill path
# ============================================================================

_TECH_JOB_DESC = "Join our engineering team building modern web applications."


class TestLLMFamilyExpansion:
    """LLM-claimed technologies must respect the SAME family-expansion rules
    (principal_skill.py's _LANGUAGE_FAMILIES / _user_knows_tech) the heuristic
    title-based detector already applies — no hard-coded React->JS mapping,
    just reuse of the one canonical family map.
    """

    def _extract(self, principal_tech: str, user_skills: list[str] = ("javascript",)):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=[principal_tech],
        )
        extractor = RequirementExtractor(user_skills=list(user_skills), llm_provider=mock_llm)
        job = _job(title="Frontend Engineer", description=f"{_TECH_JOB_DESC} Strong {principal_tech} skills required.")
        return extractor.extract(job), job

    def test_react_principal_not_rejected_for_javascript_candidate(self):
        req, job = self._extract("React")
        assert "react" not in req.principal_skill_mismatches
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert result.passes, "React is JS-family; a JavaScript candidate must not be rejected for it"

    def test_vue_principal_not_rejected_for_javascript_candidate(self):
        req, job = self._extract("Vue")
        assert "vue" not in req.principal_skill_mismatches
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert result.passes, "Vue is JS-family; a JavaScript candidate must not be rejected for it"

    def test_angular_principal_not_rejected_for_javascript_candidate(self):
        req, job = self._extract("Angular")
        assert "angular" not in req.principal_skill_mismatches
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert result.passes, "Angular is JS-family; a JavaScript candidate must not be rejected for it"

    def test_unrelated_rust_still_rejected_for_python_candidate(self):
        """Regression: family-expansion must not become a blanket bypass."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Backend Engineer", description=f"{_TECH_JOB_DESC} Strong Rust skills required.")
        req = extractor.extract(job)

        assert "rust" in req.principal_skill_mismatches
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert not result.passes
        assert any(r.type == "principal_skill" and r.requirement == "rust" for r in result.reasons)

    def test_unrelated_rust_still_rejected_for_javascript_candidate(self):
        """A JS candidate is not exempt from every hard-reject — only from JS-family techs."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["javascript"], llm_provider=mock_llm)
        job = _job(title="Backend Engineer", description=f"{_TECH_JOB_DESC} Strong Rust skills required.")
        req = extractor.extract(job)

        assert "rust" in req.principal_skill_mismatches
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert not result.passes

    def test_required_skill_react_not_penalized_for_javascript_candidate(self):
        """The same family check applies to required_skills, not just principal_technologies."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(required_skills=["React"])
        extractor = RequirementExtractor(user_skills=["javascript"], llm_provider=mock_llm)
        job = _job(title="Frontend Engineer", description=f"{_TECH_JOB_DESC} React experience required.")
        req = extractor.extract(job)

        assert not any(s.skill.lower() == "react" for s in req.skills), (
            "A JS candidate already covers React via family expansion — no requirement should be recorded"
        )
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert result.passes


# ============================================================================
# Fix 2 — generic/non-technical phrase noise reduction (prompt-level)
# ============================================================================

class TestGenericSkillNoiseGuidance:
    """The prompt fix is inherently a real-model behavior change (see the
    session's live LM Studio verification for the empirical before/after);
    these tests guard the prompt's content so the guidance can't silently
    regress, and confirm the parser still handles a WELL-BEHAVED response
    (one that already follows the tightened contract) correctly end-to-end.
    """

    def test_prompt_instructs_against_generic_duty_phrases(self):
        from src.requirements import extractor as ext_module
        prompt = ext_module._LLM_EXTRACTION_PROMPT
        for bad_example in ("pick", "physical stamina", "communication", "resolve problems"):
            assert bad_example in prompt, f"prompt should list {bad_example!r} as a BAD example"
        for good_example in ("Kubernetes", "PostgreSQL", "SCADA", "UiPath"):
            assert good_example in prompt, f"prompt should list {good_example!r} as a GOOD example"

    def test_well_behaved_response_with_no_technical_content_yields_nothing(self):
        """When the LLM correctly follows the contract (empty lists for a
        non-technical job), no spurious skill/penalty is recorded."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json()  # all empty, as instructed
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(
            title="Warehouse Operative",
            description="Pick and pack orders in our engineering-adjacent logistics team. Physical stamina required.",
        )
        req = extractor.extract(job)
        assert req.skills == []
        assert req.principal_skill_mismatches == []

    def test_real_technology_among_noise_is_still_kept(self):
        """A well-behaved response naming one real tool alongside a job with
        duty-heavy phrasing must still surface that tool -- precision fix
        must not blanket-suppress genuine detections."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(preferred_skills=["Canva"])
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(
            title="Marketing Coordinator",
            description="Plan marketing campaigns for our engineering department. Experience with Canva is a plus.",
        )
        req = extractor.extract(job)
        assert any(s.skill == "Canva" and s.source == EvidenceSource.LLM_CORROBORATED for s in req.skills)


# ============================================================================
# Fix 3 — selective invocation (should_run_llm)
# ============================================================================

class TestSelectiveInvocation:
    def _heuristic(self, job: Job, user_skills: list[str] = ("python",)):
        return RequirementExtractor(user_skills=list(user_skills))._extract_heuristic(job)

    def test_skips_when_heuristic_already_found_principal_mismatch(self):
        job = _job(title="Rust Developer", description="Rust developer wanted.")
        h = self._heuristic(job)
        assert h.principal_skill_mismatches  # sanity: heuristic found it
        assert should_run_llm(job, h) is False

    def test_skips_when_heuristic_already_found_mandatory_education(self):
        job = _job(description="Bac+5 obligatoire. Python developer role.")
        h = self._heuristic(job)
        assert h.education is not None and h.education.importance.value == "mandatory"
        assert should_run_llm(job, h) is False

    def test_calls_when_education_ambiguous_and_context_present(self):
        job = _job(description="Ideally you hold a relevant university degree. Python role.")
        h = self._heuristic(job)
        assert should_run_llm(job, h) is True

    def test_calls_when_no_principal_signal_but_technical_context_present(self):
        job = _job(title="Backend Engineer", description="Join our engineering team building backend services.")
        h = self._heuristic(job)
        assert not h.principal_skill_mismatches
        assert should_run_llm(job, h) is True

    def test_skips_when_no_signal_at_all(self):
        job = _job(title="Office Manager", description="Great benefits and a friendly team environment.")
        h = self._heuristic(job)
        assert should_run_llm(job, h) is False

    def test_extract_does_not_call_llm_when_predicate_says_skip(self):
        job = _job(title="Rust Developer", description="Rust developer wanted.")
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json()
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        result = extractor.extract(job)

        mock_llm.complete.assert_not_called()
        assert result.extraction_method == "heuristic"
        assert "rust" in result.principal_skill_mismatches  # heuristic decision preserved

    def test_extract_calls_llm_when_predicate_says_go(self):
        job = _job(title="Backend Engineer", description="Join our engineering team building backend services.")
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json()
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        extractor.extract(job)

        mock_llm.complete.assert_called_once()

    def test_selective_invocation_never_weakens_heuristic_hard_reject(self):
        """End-to-end: even though the LLM is skipped, the heuristic's own
        violation still reaches the gate exactly as if the LLM had run."""
        job = _job(title="Rust Developer", description="Rust developer wanted. Bac+5 requis.")
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json()  # would never be reached
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        req = extractor.extract(job)

        mock_llm.complete.assert_not_called()
        result = HardConstraintGate(user_max_education_level=2).check(req)
        assert not result.passes
        assert {r.type for r in result.reasons} == {"education", "principal_skill"}


# ============================================================================
# Real-technical-recall regression — indirect FR/EN phrasing must keep working
# through all three fixes (family expansion, tightened prompt, selectivity).
# Canned responses mirror what the real Qwen3-8B model actually returned for
# these exact sentences during this session's live LM Studio validation.
# ============================================================================

class TestIndirectTechnicalRecallRegression:
    def test_indirect_rust_english(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            required_skills=["Rust"], is_principal_tech_role=True, principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description="A strong understanding of Rust is expected for this role.")
        req = extractor.extract(job)

        mock_llm.complete.assert_called_once()  # predicate must let this one through
        assert "rust" in req.principal_skill_mismatches
        result = HardConstraintGate(user_max_education_level=5).check(req)
        assert not result.passes

    def test_indirect_python_english(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            required_skills=["Python"], is_principal_tech_role=True, principal_technologies=["Python"],
        )
        extractor = RequirementExtractor(user_skills=["javascript"], llm_provider=mock_llm)
        job = _job(description="You will spend most of your time developing in Python.")
        req = extractor.extract(job)

        mock_llm.complete.assert_called_once()
        assert "python" in req.principal_skill_mismatches

    def test_indirect_java_english_preferred(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(preferred_skills=["Java"], is_principal_tech_role=True)
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description="Experience with Java would be advantageous.")
        req = extractor.extract(job)

        mock_llm.complete.assert_called_once()
        assert any(s.skill == "Java" and s.importance == Importance.PREFERRED for s in req.skills)

    def test_indirect_rust_french(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            required_skills=["Rust"], is_principal_tech_role=True, principal_technologies=["Rust"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description="Une solide maitrise de Rust est indispensable pour ce poste.")
        req = extractor.extract(job)

        mock_llm.complete.assert_called_once()
        assert "rust" in req.principal_skill_mismatches

    def test_indirect_python_french_principalement(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            required_skills=["Python"], is_principal_tech_role=True, principal_technologies=["Python"],
        )
        extractor = RequirementExtractor(user_skills=["javascript"], llm_provider=mock_llm)
        job = _job(description="Vous serez amene a developper principalement en Python au quotidien.")
        req = extractor.extract(job)

        mock_llm.complete.assert_called_once()
        assert "python" in req.principal_skill_mismatches


# ============================================================================
# Test 8 — Domain vocabulary is never treated as an unknown principal tech
# (2026-09-05 real-job benchmark: Jobs 3/19/31)
# ============================================================================

class TestDomainVocabularyNotUnknownTech:
    """The LLM must never treat the candidate's own target_domains (or close
    synonyms/family members) as an unknown 'principal technology' — the
    benchmark found "système"/"réseau" (a technicien informatique job),
    "pentest"/"sécurité réseau" (a Thales pentest role), and "antivirus"/
    "SIEM" (a cybersecurity ops role) all wrongly flagged as unknown tech
    requiring hard rejection, despite each being the candidate's own target
    domain."""

    def test_reseau_and_systeme_are_not_unknown_tech(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True, principal_technologies=["système", "réseau"],
        )
        extractor = RequirementExtractor(
            user_skills=["python"], llm_provider=mock_llm,
            target_domains=["réseaux", "systèmes"],
        )
        job = _job(description="Vous interviendrez sur le système et le réseau de l'entreprise.")
        req = extractor.extract(job)

        assert req.principal_skill_mismatches == []

    def test_pentest_and_reseau_security_are_not_unknown_tech(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["pentest", "sécurité réseau"],
        )
        extractor = RequirementExtractor(
            user_skills=["python"], llm_provider=mock_llm,
            target_domains=["cybersécurité"],
        )
        job = _job(description="Vous réaliserez des missions de pentest et de sécurité réseau.")
        req = extractor.extract(job)

        assert req.principal_skill_mismatches == []

    def test_antivirus_and_siem_are_not_unknown_tech(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True,
            principal_technologies=["antivirus", "SIEM"],
        )
        extractor = RequirementExtractor(
            user_skills=["python"], llm_provider=mock_llm,
            target_domains=["cybersécurité"],
        )
        job = _job(description="Administration des solutions antivirus et du SIEM.")
        req = extractor.extract(job)

        assert req.principal_skill_mismatches == []

    def test_unrelated_unknown_tech_still_flagged(self):
        """Sanity check: the domain-family guard must not swallow genuinely
        unrelated unknown technologies."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True, principal_technologies=["Kotlin"],
        )
        extractor = RequirementExtractor(
            user_skills=["python"], llm_provider=mock_llm,
            target_domains=["réseaux", "cybersécurité"],
        )
        job = _job(description="Développement mobile natif en Kotlin.")
        req = extractor.extract(job)

        assert "kotlin" in req.principal_skill_mismatches


# ============================================================================
# Test 9 — Generic role/duty words are never treated as a named technology
# (2026-09-05 real-job benchmark: Job 13)
# ============================================================================

class TestGenericTermsNotTreatedAsTech:
    """Generic role/duty/category words (front-end, back-end, database, ...)
    must never become rejection/penalty evidence, even when the LLM names
    them in violation of its own prompt instructions."""

    def test_frontend_backend_database_are_dropped(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            required_skills=["front-end", "back-end", "bases de données"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description="Vous travaillerez sur le front-end, le back-end et les bases de données.")
        req = extractor.extract(job)

        assert req.skills == []

    def test_generic_term_as_principal_tech_is_dropped(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True, principal_technologies=["backend"],
        )
        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(description="Poste centré sur le backend de notre plateforme.")
        req = extractor.extract(job)

        assert req.principal_skill_mismatches == []


# ============================================================================
# Test 10 — Inclusive/alternative technology lists are not hard rejections
# (2026-09-05 real-job benchmark: Job 32, the worst regression in that sample)
# ============================================================================

class TestInclusiveTechnologyListIsNotHardRejected:
    """A posting offering 'plusieurs technologies parmi Java/PHP/JavaScript/
    Python/Node/...' means the candidate needs ONE of them, not all. If the
    candidate already knows a different item from the SAME list, naming one
    they lack must not become a mandatory-missing/principal-mismatch
    requirement — the benchmark's worst regression was exactly this: a
    junior-friendly, explicitly inclusive posting collapsed to a single
    mandatory Java pick and wrongly hard-rejected."""

    def test_inclusive_list_principal_tech_not_rejected_when_candidate_knows_alternative(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True, principal_technologies=["Java"],
        )
        extractor = RequirementExtractor(user_skills=["python", "javascript", "php"], llm_provider=mock_llm)
        job = _job(
            description=(
                "Poste ouvert aux profils juniors : plusieurs technologies parmi "
                "Java/PHP/JavaScript/Python/Node sont acceptées."
            ),
        )
        req = extractor.extract(job)

        assert req.principal_skill_mismatches == []

    def test_inclusive_list_required_skill_not_penalized_when_candidate_knows_alternative(self):
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(required_skills=["Java"])
        extractor = RequirementExtractor(user_skills=["python", "javascript", "php"], llm_provider=mock_llm)
        job = _job(
            description=(
                "Plusieurs technologies parmi Java, PHP, JavaScript, Python sont "
                "acceptées selon votre profil."
            ),
        )
        req = extractor.extract(job)

        assert req.skills == []

    def test_exclusive_requirement_outside_a_list_is_still_flagged(self):
        """Sanity check: without inclusive-list phrasing, a genuinely missing
        principal technology must still be flagged as before."""
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _llm_json(
            is_principal_tech_role=True, principal_technologies=["Java"],
        )
        extractor = RequirementExtractor(user_skills=["python", "javascript", "php"], llm_provider=mock_llm)
        job = _job(description="Ce poste est exclusivement centré sur Java.")
        req = extractor.extract(job)

        assert "java" in req.principal_skill_mismatches
