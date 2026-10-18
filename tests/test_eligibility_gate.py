"""Tests for the lightweight LLM semantic eligibility gate (src/ai/eligibility.py).

No real LM Studio/Ollama server or network is required — the LLM provider is
mocked throughout (its public ``complete()`` primitive, the same seam
``RequirementExtractor`` uses for its own LLM enrichment).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.ai.eligibility import EligibilityGate, _assemble_evidence, _parse_decision
from src.ai.models import BatchEligibilityResult, EligibilityDecision, EligibilityResult
from src.ai.storage import AIAnalysisStorage
from src.models.job import Job
from src.requirements.models import (
    EducationRequirement,
    EvidenceSource,
    ExperienceRequirement,
    Importance,
    JobRequirements,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def job_ingenieur() -> Job:
    """A title that conventionally implies Bac+5 with nothing explicit stated."""
    return Job(
        id="job-ingenieur-001",
        source="test",
        title="Ingénieur logiciel",
        company="Acme",
        description="Nous recherchons un ingénieur logiciel pour rejoindre notre équipe technique.",
        location="Paris, France",
        countries=["France"],
        remote=False,
        employment_type="full-time",
        discovered_at=datetime.now(UTC),
        content_hash="hash-v1",
    )


@pytest.fixture
def job_alternance() -> Job:
    """Same-sounding title, but alternance context should make it accessible."""
    return Job(
        id="job-alternance-001",
        source="test",
        title="Ingénieur logiciel en alternance",
        company="Acme",
        description="Alternance BTS/DUT bienvenue, poste ouvert aux étudiants en apprentissage.",
        location="Paris, France",
        countries=["France"],
        remote=False,
        employment_type="alternance",
        discovered_at=datetime.now(UTC),
        content_hash="hash-v1",
    )


@pytest.fixture
def empty_profile() -> dict[str, Any]:
    return {
        "max_education": "bac+2",
        "experience": {"max_years": 3, "preferred_levels": ["junior", "internship", "alternance"]},
    }


def _mock_provider(response_text: str) -> MagicMock:
    provider = MagicMock()
    provider.complete.return_value = response_text
    provider.model_name = "test-model"
    return provider


# ---------------------------------------------------------------------------
# _assemble_evidence — prefers structured evidence over raw description
# ---------------------------------------------------------------------------

class TestAssembleEvidence:
    def test_prefers_education_span_over_description_dump(self, job_ingenieur):
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.MANDATORY,
                raw_text="Bac+5 exigé",
                source=EvidenceSource.HEURISTIC,
            ),
        )
        evidence, has_evidence = _assemble_evidence(job_ingenieur, requirements)
        assert "Bac+5 exigé" in evidence
        # The full raw description must NOT be dumped verbatim when real
        # evidence already exists — that's the whole point of using
        # deterministic spans instead of description[:N].
        assert "rejoindre notre équipe technique" not in evidence
        assert has_evidence is True

    def test_includes_experience_evidence(self, job_ingenieur):
        requirements = JobRequirements(
            experience=ExperienceRequirement(
                min_years=5, importance=Importance.MANDATORY, raw_text="5 ans d'expérience minimum",
            ),
        )
        evidence, has_evidence = _assemble_evidence(job_ingenieur, requirements)
        assert "5 ans d'expérience minimum" in evidence
        assert has_evidence is True

    def test_surfaces_seniority_indicator_words(self):
        job = Job(
            id="job-senior-001", source="test", title="Senior Software Engineer",
            company="Acme", description="Looking for a senior engineer to lead the team.",
            location="Remote", countries=[], remote=True, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        evidence, has_evidence = _assemble_evidence(job, None)
        assert "senior" in evidence.lower()
        assert has_evidence is True

    def test_surfaces_alternance_context(self, job_alternance):
        evidence, has_evidence = _assemble_evidence(job_alternance, None)
        assert "alternance" in evidence.lower() or "apprenti" in evidence.lower()
        assert has_evidence is True

    def test_falls_back_to_description_only_when_no_evidence_at_all(self):
        job = Job(
            id="job-neutral-001", source="test", title="Technicien informatique",
            company="Acme", description="Support utilisateurs et maintenance de parc informatique.",
            location="Paris", countries=["France"], remote=False, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        evidence, has_evidence = _assemble_evidence(job, None)
        assert "Support utilisateurs" in evidence
        assert has_evidence is False, "generic description fallback is not structured evidence"

    def test_stays_compact(self, job_ingenieur):
        """Evidence text must stay well under a full-description dump."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.MANDATORY,
                raw_text="Bac+5 exigé pour ce poste",
            ),
        )
        evidence, _has_evidence = _assemble_evidence(job_ingenieur, requirements)
        assert len(evidence) < 500


# ---------------------------------------------------------------------------
# _parse_decision — never raises, defaults to UNCERTAIN on any malformation
# ---------------------------------------------------------------------------

class TestParseDecision:
    def test_valid_json(self):
        d = _parse_decision('{"decision":"REJECT","confidence":0.9,"reason":"role_level_above_profile"}')
        assert d.decision == "REJECT"
        assert d.confidence == 0.9
        assert d.reason == "role_level_above_profile"

    def test_lowercase_decision_normalized(self):
        d = _parse_decision('{"decision":"pass","confidence":0.8,"reason":"ok"}')
        assert d.decision == "PASS"

    def test_markdown_fenced_json(self):
        d = _parse_decision('```json\n{"decision":"PASS","confidence":0.7,"reason":"ok"}\n```')
        assert d.decision == "PASS"

    def test_json_with_surrounding_text(self):
        d = _parse_decision('Sure, here is the answer: {"decision":"UNCERTAIN","confidence":0.4,"reason":"ambiguous"} thanks')
        assert d.decision == "UNCERTAIN"

    def test_malformed_json_defaults_to_uncertain(self):
        d = _parse_decision("not json at all")
        assert d.decision == "UNCERTAIN"
        assert d.confidence == 0.0

    def test_empty_string_defaults_to_uncertain(self):
        d = _parse_decision("")
        assert d.decision == "UNCERTAIN"

    def test_invalid_decision_value_defaults_to_uncertain(self):
        d = _parse_decision('{"decision":"MAYBE","confidence":0.5,"reason":"x"}')
        assert d.decision == "UNCERTAIN"

    def test_never_raises_on_garbage(self):
        for garbage in ["{{{", "null", "[]", "12345", '{"confidence": "high"}']:
            _parse_decision(garbage)  # must not raise


# ---------------------------------------------------------------------------
# EligibilityGate.evaluate_job / evaluate_batch
# ---------------------------------------------------------------------------

class TestEvaluateJob:
    def test_successful_evaluation(self, job_ingenieur, empty_profile):
        provider = _mock_provider('{"decision":"REJECT","confidence":0.92,"reason":"role_level_above_profile"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        result = gate.evaluate_job(job_ingenieur, None, empty_profile)
        assert result.status == "success"
        assert result.decision.decision == "REJECT"
        assert result.decision.confidence == 0.92

    def test_llm_exception_is_caught_as_failed_status(self, job_ingenieur, empty_profile):
        provider = MagicMock()
        provider.complete.side_effect = RuntimeError("connection refused")
        provider.model_name = "test-model"
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        result = gate.evaluate_job(job_ingenieur, None, empty_profile)
        assert result.status == "failed"
        assert result.error_message is not None

    def test_empty_response_is_failed_not_uncertain_pass(self, job_ingenieur, empty_profile):
        provider = _mock_provider("")
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        result = gate.evaluate_job(job_ingenieur, None, empty_profile)
        assert result.status == "failed"

    def test_evaluate_batch_sequential_and_concurrent_agree(self, job_ingenieur, job_alternance, empty_profile):
        """max_workers=1 vs max_workers=4 must produce the same per-job results."""
        provider_seq = _mock_provider('{"decision":"PASS","confidence":0.8,"reason":"ok"}')
        provider_par = _mock_provider('{"decision":"PASS","confidence":0.8,"reason":"ok"}')
        jobs = [job_ingenieur, job_alternance]

        gate_seq = EligibilityGate(llm_provider=provider_seq, max_workers=1)
        gate_par = EligibilityGate(llm_provider=provider_par, max_workers=4)

        results_seq = gate_seq.evaluate_batch(jobs, {}, empty_profile)
        results_par = gate_par.evaluate_batch(jobs, {}, empty_profile)

        assert [r.job_id for r in results_seq] == [r.job_id for r in results_par] == [j.id for j in jobs]
        assert [r.decision.decision for r in results_seq] == [r.decision.decision for r in results_par]

    def test_evaluate_batch_never_exceeds_configured_workers(self, job_ingenieur, job_alternance, empty_profile):
        import threading

        max_concurrent = 0
        current = 0
        lock = threading.Lock()

        def slow_call(prompt: str) -> str:
            nonlocal max_concurrent, current
            with lock:
                current += 1
                max_concurrent = max(max_concurrent, current)
            import time
            time.sleep(0.05)
            with lock:
                current -= 1
            return '{"decision":"PASS","confidence":0.8,"reason":"ok"}'

        provider = MagicMock()
        provider.complete.side_effect = slow_call
        provider.model_name = "test-model"

        gate = EligibilityGate(llm_provider=provider, max_workers=2)
        jobs = [job_ingenieur, job_alternance] * 3  # 6 calls, 2 workers
        gate.evaluate_batch(jobs, {}, empty_profile)

        assert max_concurrent <= 2


# ---------------------------------------------------------------------------
# EligibilityGate.run — the conservative consumption rule
# ---------------------------------------------------------------------------

class TestConsumptionRule:
    def test_high_confidence_reject_removes_job(self, job_ingenieur, empty_profile, tmp_path):
        """A REJECT backed by real, corroborating evidence removes the job."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.MANDATORY,
                raw_text="Bac+5 exigé",
            ),
        )
        provider = _mock_provider('{"decision":"REJECT","confidence":0.95,"reason":"role_level_above_profile"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert survivors == []
        assert stats.rejected == 1

    def test_high_confidence_reject_without_evidence_is_downgraded(self, job_ingenieur, empty_profile, tmp_path):
        """A REJECT with NO corroborating evidence at all — no seniority word in
        the title/description, no education/experience requirement extracted —
        is an unsupported free-form hallucination and must not remove the job.
        Reproduces the 2026-09-05 real-job benchmark's Jobs 1/16 finding, where
        the eligibility gate stated 'title implies seniority' on titles that
        contain zero seniority words."""
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.95,"reason":"title_seniority_overrides_profile"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.rejected == 0
        assert stats.unsupported_reject == 1

    def test_low_confidence_reject_passes_through(self, job_ingenieur, empty_profile, tmp_path):
        provider = _mock_provider('{"decision":"REJECT","confidence":0.5,"reason":"maybe_above_profile"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.rejected == 0

    def test_uncertain_always_passes_through(self, job_ingenieur, empty_profile, tmp_path):
        provider = _mock_provider('{"decision":"UNCERTAIN","confidence":0.99,"reason":"ambiguous"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.uncertain == 1
        assert stats.rejected == 0

    def test_llm_failure_fails_open(self, job_ingenieur, empty_profile, tmp_path):
        provider = MagicMock()
        provider.complete.side_effect = RuntimeError("boom")
        provider.model_name = "test-model"
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1  # never removed on a gate failure
        assert stats.failed == 1

    def test_gate_never_mutates_job_requirements(self, job_ingenieur, empty_profile, tmp_path):
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.MANDATORY,
                raw_text="Bac+5 exigé",
            ),
        )
        snapshot = requirements.education.raw_text, requirements.education.importance

        provider = _mock_provider('{"decision":"REJECT","confidence":0.95,"reason":"x"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage,
            reject_confidence=0.75,
        )

        assert (requirements.education.raw_text, requirements.education.importance) == snapshot


# ---------------------------------------------------------------------------
# Deterministic modality precedence — AI cannot override a PREFERRED/OPTIONAL/
# UNKNOWN/OR-alternative classification into a hard rejection (2026-09-05
# real-job benchmark: EligibilityGate rejected ISAGRI/Thales BackEnd/Thales
# Pentest/TELESPAZIO on "Bac+5" mentions RequirementExtractor had already
# correctly classified as PREFERRED or an OR-alternative, and
# HardConstraintGate had already correctly let through as a soft signal).
# ---------------------------------------------------------------------------

class TestDeterministicModalityPrecedence:
    def test_preferred_education_reject_is_downgraded(self, job_ingenieur, empty_profile, tmp_path):
        """Candidate has Bac+2; posting says Bac+5 is PREFERRED; AI rejects
        anyway citing education — must NOT be hard-rejected."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.PREFERRED,
                raw_text="Bac+5 souhaité en informatique",
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"education_requirement_bac_plus_5"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.rejected == 0
        assert stats.unsupported_reject == 1

    def test_or_alternative_education_reject_is_downgraded(self, job_ingenieur, empty_profile, tmp_path):
        """'Bac+5 ou équivalent' is classified as an OR-alternative — the AI
        calling it mandatory must not override that modality."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.PREFERRED,
                raw_text="Bac+5 ou équivalent en informatique", is_or_alternative=True,
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.95,"reason":"education_requirement_bac_plus_5_candidate_has_bac_plus_2"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1

    def test_unknown_education_reject_is_downgraded(self, job_ingenieur, empty_profile, tmp_path):
        """UNKNOWN importance (heuristic found the level but no qualifier) —
        AI resolving it as mandatory with no separate evidence must not stand."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.UNKNOWN,
                raw_text="Notre équipe accueille des profils Bac+5",
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"title_implied_higher_education"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1

    def test_mandatory_education_reject_still_stands(self, job_ingenieur, empty_profile, tmp_path):
        """'Bac+5 obligatoire' (MANDATORY) — the AI agreeing must still remove
        the job. In production this job would already have been rejected by
        HardConstraintGate before ever reaching EligibilityGate; this test
        confirms the new precedence check does not itself block a genuine
        MANDATORY case if one somehow still reaches this gate."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.MANDATORY,
                raw_text="Bac+5 obligatoire pour ce poste",
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"education_requirement_bac_plus_5"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert survivors == []
        assert stats.rejected == 1
        assert stats.unsupported_reject == 0

    def test_preferred_experience_reject_is_downgraded(self, job_ingenieur, empty_profile, tmp_path):
        """Same precedence rule for experience, not just education."""
        requirements = JobRequirements(
            experience=ExperienceRequirement(
                min_years=5, importance=Importance.PREFERRED, raw_text="5 ans d'expérience souhaités",
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"experience_requirement_5_years"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1

    def test_mandatory_experience_reject_still_stands(self, job_ingenieur, empty_profile, tmp_path):
        requirements = JobRequirements(
            experience=ExperienceRequirement(
                min_years=7, importance=Importance.MANDATORY, raw_text="7 ans d'expérience minimum requis",
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"experience_requirement_7_years"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {job_ingenieur.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert survivors == []
        assert stats.rejected == 1

    def test_unrelated_reason_with_soft_education_present_still_rejects(self, job_ingenieur, empty_profile, tmp_path):
        """A REJECT for a reason that isn't about education/experience at all
        (e.g. a genuine, evidenced title-seniority signal) must NOT be
        downgraded just because the job also happens to carry an unrelated
        soft education mention."""
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.PREFERRED,
                raw_text="Bac+5 souhaité",
            ),
        )
        job = Job(
            id="job-senior-title", source="test", title="Senior Software Engineer",
            company="Acme", description="Looking for a senior engineer to lead the team.",
            location="Paris", countries=["France"], remote=False, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"explicit_senior_title"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, decisions, stats = gate.run(
            [job], {job.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert survivors == []
        assert stats.rejected == 1

    # -- Real benchmark job reproductions --------------------------------

    def test_isagri_preferred_or_alt_education_reject_downgraded(self, empty_profile, tmp_path):
        """Real ISAGRI job: 'Bac+5 en informatique, avec une expérience...'
        classified PREFERRED + OR-alternative; AI rejected citing education."""
        job = Job(
            id="isagri", source="test", title="Développeur Full-Stack Node/VueJS H/F",
            company="ISAGRI", description="profil recherché", location="France",
            countries=["France"], remote=False, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.PREFERRED,
                raw_text="PROFIL RECHERCHE\n- Bac+5 en informatique, avec une exp",
                is_or_alternative=True,
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"education_requirement_bac_plus_5"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, _decisions, stats = gate.run(
            [job], {job.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1

    def test_thales_backend_preferred_or_alt_education_reject_downgraded(self, empty_profile, tmp_path):
        """Real Thales BackEnd job: 'De formation Bac+5 en Informatique
        (école d'ingénieur)' classified PREFERRED + OR-alternative."""
        job = Job(
            id="thales-backend", source="test", title="Développeur BackEnd - Traitement de Données (F/H)",
            company="Thales", description="profil", location="France",
            countries=["France"], remote=False, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.PREFERRED,
                raw_text="De formation Bac +5 en Informatique (école d'ingé",
                is_or_alternative=True,
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"education_requirement_bac_plus_5_candidate_has_bac_plus_2"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, _decisions, stats = gate.run(
            [job], {job.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1

    def test_thales_pentest_unknown_education_reject_downgraded(self, empty_profile, tmp_path):
        """Real Thales Pentest job: 'Vous disposez d'un Bac+5 et/ou Ingénieur
        informatique' classified UNKNOWN importance."""
        job = Job(
            id="thales-pentest", source="test", title="Evaluateur Pentest Sécurité Réseau F/H",
            company="Thales", description="profil", location="France",
            countries=["France"], remote=False, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.UNKNOWN,
                raw_text="Vous disposez d'un Bac+5 et/ou Ingénieur informatique",
            ),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.9,"reason":"education_requirement_bac_plus_5"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, _decisions, stats = gate.run(
            [job], {job.id: requirements}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1

    def test_telespazio_no_evidence_reject_downgraded(self, empty_profile, tmp_path):
        """Real TELESPAZIO job: no genuine education/experience requirement
        text at all; AI inferred 'title implies higher education' from pure
        title convention — caught by the pre-existing zero-evidence guard."""
        job = Job(
            id="telespazio", source="test", title="Ingénieur Cybersécurité et Opérations (F/H)",
            company="Telespazio",
            description="Telespazio France est le leader europeen des services satellitaires.",
            location="France", countries=["France"], remote=False, employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )
        provider = _mock_provider(
            '{"decision":"REJECT","confidence":0.95,"reason":"title_implied_higher_education"}'
        )
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        survivors, _decisions, stats = gate.run(
            [job], {}, empty_profile, storage, reject_confidence=0.75,
        )
        assert len(survivors) == 1
        assert stats.unsupported_reject == 1


# ---------------------------------------------------------------------------
# Caching — content_hash / profile_version aware (via AIAnalysisStorage)
# ---------------------------------------------------------------------------

class TestEligibilityCaching:
    def test_second_run_with_same_content_and_profile_hits_cache(self, job_ingenieur, empty_profile, tmp_path):
        provider = _mock_provider('{"decision":"PASS","confidence":0.8,"reason":"ok"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        gate.run([job_ingenieur], {}, empty_profile, storage, profile_version="p1")
        assert provider.complete.call_count == 1

        survivors, decisions, stats = gate.run(
            [job_ingenieur], {}, empty_profile, storage, profile_version="p1",
        )
        assert provider.complete.call_count == 1  # no new call — served from cache
        assert stats.cached == 1

    def test_content_hash_change_busts_cache(self, job_ingenieur, empty_profile, tmp_path):
        provider = _mock_provider('{"decision":"PASS","confidence":0.8,"reason":"ok"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        gate.run([job_ingenieur], {}, empty_profile, storage, profile_version="p1")
        assert provider.complete.call_count == 1

        job_ingenieur.content_hash = "hash-v2"  # description was edited/re-scraped
        gate.run([job_ingenieur], {}, empty_profile, storage, profile_version="p1")
        assert provider.complete.call_count == 2  # re-evaluated, not served stale

    def test_profile_version_change_busts_cache(self, job_ingenieur, empty_profile, tmp_path):
        provider = _mock_provider('{"decision":"PASS","confidence":0.8,"reason":"ok"}')
        gate = EligibilityGate(llm_provider=provider, max_workers=1)
        storage = AIAnalysisStorage(db_path=str(tmp_path / "test.db"))
        storage.init_db()

        gate.run([job_ingenieur], {}, empty_profile, storage, profile_version="p1")
        assert provider.complete.call_count == 1

        gate.run([job_ingenieur], {}, empty_profile, storage, profile_version="p2")
        assert provider.complete.call_count == 2


# ---------------------------------------------------------------------------
# EligibilityGate.run_stage — the pipeline-stage entry point (mirrors
# src/pipeline/preflight.py's run_preflight pattern). Provider construction,
# rejection-record construction, and stats bookkeeping all live here now
# instead of inline in src/pipeline/orchestrator.py — see
# tests/test_eligibility_gate_orchestrator_wiring.py for the orchestrator
# side of this boundary.
# ---------------------------------------------------------------------------

def _fake_profile(enabled: bool = False, reject_confidence: float = 0.75):
    from types import SimpleNamespace
    return SimpleNamespace(
        matching=SimpleNamespace(
            llm_eligibility_enabled=enabled,
            llm_eligibility_reject_confidence=reject_confidence,
        ),
        model_dump=lambda: {},
    )


class TestRunStage:
    def test_disabled_returns_jobs_unchanged_without_constructing_provider(self, job_ingenieur, tmp_path):
        from src.ai.interfaces import LocalLLMProvider

        with patch.object(LocalLLMProvider, "__init__") as spy_init:
            result = EligibilityGate.run_stage(
                [job_ingenieur], {}, _fake_profile(enabled=False), "hash1",
                str(tmp_path / "test.db"), use_ai=True,
            )

        spy_init.assert_not_called()
        assert result.ran is False
        assert result.survivors == [job_ingenieur]
        assert result.stats == {}
        assert result.rejected == 0
        assert result.passed == 1

    def test_use_ai_false_never_constructs_provider_even_when_enabled(self, job_ingenieur, tmp_path):
        from src.ai.interfaces import LocalLLMProvider

        with patch.object(LocalLLMProvider, "__init__") as spy_init:
            result = EligibilityGate.run_stage(
                [job_ingenieur], {}, _fake_profile(enabled=True), "hash1",
                str(tmp_path / "test.db"), use_ai=False,
            )

        spy_init.assert_not_called()
        assert result.ran is False

    def test_no_profile_never_constructs_provider(self, job_ingenieur, tmp_path):
        from src.ai.interfaces import LocalLLMProvider

        with patch.object(LocalLLMProvider, "__init__") as spy_init:
            result = EligibilityGate.run_stage(
                [job_ingenieur], {}, None, "hash1",
                str(tmp_path / "test.db"), use_ai=True,
            )

        spy_init.assert_not_called()
        assert result.ran is False
        assert result.survivors == [job_ingenieur]

    def test_enabled_builds_rejection_records_and_stats_from_gate_run(self, job_ingenieur, tmp_path):
        reject_decision = EligibilityDecision(decision="REJECT", confidence=0.9, reason="too senior")
        fake_result = EligibilityResult(
            job_id=job_ingenieur.id, decision=reject_decision,
            prompt_version="eligibility_gate_v1", model_identifier="m", status="success",
        )
        fake_batch_stats = BatchEligibilityResult(
            total_jobs=1, successful=1, failed=0, rejected=1, results=[fake_result],
        )

        with patch.object(EligibilityGate, "run", return_value=([], {job_ingenieur.id: fake_result}, fake_batch_stats)):
            result = EligibilityGate.run_stage(
                [job_ingenieur], {}, _fake_profile(enabled=True, reject_confidence=0.8), "hash1",
                str(tmp_path / "test.db"), use_ai=True,
            )

        assert result.ran is True
        assert result.survivors == []
        assert result.rejected == 1
        assert result.passed == 0
        assert len(result.rejection_records) == 1
        rec = result.rejection_records[0]
        assert rec.job_id == job_ingenieur.id
        assert rec.rejection_stage == "llm_eligibility"
        detail = json.loads(rec.rejection_detail)
        assert detail["decision"] == "REJECT"
        assert detail["reason"] == "too senior"
        assert result.stats["rejected"] == 1

    def test_provider_construction_failure_fails_open(self, job_ingenieur, tmp_path, monkeypatch):
        """Any failure inside run_stage (config load, provider construction,
        the gate's own .run()) must fail open — every input job survives
        unchanged, never lost to an eligibility-gate-internal error."""
        import src.ai.config as ai_config

        def _boom():
            raise RuntimeError("config load boom")

        monkeypatch.setattr(ai_config, "load_phase2_config", _boom)

        result = EligibilityGate.run_stage(
            [job_ingenieur], {}, _fake_profile(enabled=True), "hash1",
            str(tmp_path / "test.db"), use_ai=True,
        )

        assert result.ran is False
        assert result.survivors == [job_ingenieur]
        assert result.error == "config load boom"
        assert result.stats == {"error": "config load boom"}
        assert result.rejection_records == []
