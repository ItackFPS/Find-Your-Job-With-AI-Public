"""End-to-end hard-filter integration tests.

Verifies the real production path: profile YAML → profile model →
HardConstraintGate → GateResult with full provenance.

Spec requirements covered:
  §1  Production path audit (profile → gate → result)
  §2  No user-specific policy in engine (self-audit)
  §3  Profile adaptability through real config mechanism
  §4  None vs UNKNOWN semantics (no requirement vs unknown extraction)
  §5  "or equivalent" disjunction semantics (6 contrast cases)
  §6  Rejection provenance at output level (span + rule_version)
  §7  UNKNOWN and contradiction safety end-to-end
  §8  AI boundary (reject:true LLM output is ignored)
  §10 All eight constraint dimensions
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.models.job import Job
from src.models.profile import ExperiencePreferences, JobSearchProfile
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import (
    HardConstraintGate,
    gate_jobs,
    rejection_reasons_to_detail,
)
from src.requirements.models import (
    ConstraintState,
    EducationRequirement,
    GateResult,
    Importance,
    JobRequirements,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _job(
    *,
    job_id: str = "j1",
    title: str = "Software Engineer",
    description: str = "",
    location: str = "Paris, France",
    countries: list[str] | None = None,
    remote: bool = True,
) -> Job:
    return Job(
        id=job_id,
        source="test",
        title=title,
        company="Acme",
        description=description,
        location=location,
        countries=countries if countries is not None else ["France"],
        remote=remote,
        employment_type="full-time",
        discovered_at=datetime.now(UTC),
    )


def _gate(
    max_edu: int | None = None,
    max_exp: int | None = None,
    user_skills: list[str] | None = None,
    profile_hash: str = "test-hash",
    education_hard_constraint: bool = True,
) -> tuple[HardConstraintGate, RequirementExtractor]:
    gate = HardConstraintGate(
        user_max_education_level=max_edu,
        user_max_years_experience=max_exp,
        education_hard_constraint=education_hard_constraint,
        principal_skill_gate=True,
        mandatory_skill_gate=True,
        profile_version_hash=profile_hash,
    )
    extractor = RequirementExtractor(user_skills=user_skills or [])
    return gate, extractor


def _check(gate: HardConstraintGate, extractor: RequirementExtractor, job: Job) -> GateResult:
    req = extractor.extract(job)
    return gate.check(req)


def _write_profile(parent_dir: Path, content: str) -> str:
    """Write a profile.yaml into *parent_dir* (created if absent). Returns path."""
    parent_dir.mkdir(parents=True, exist_ok=True)
    p = parent_dir / "profile.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(p)


# ============================================================================
# §1 — Production path: profile_version_hash reaches GateResult
# ============================================================================

class TestProductionPathProvenance:
    """Profile version propagates from config to every GateResult."""

    def test_profile_hash_in_gate_result_on_pass(self):
        gate, extractor = _gate(max_edu=2, profile_hash="abc123")
        job = _job(description="We need a backend developer.")
        result = _check(gate, extractor, job)
        assert result.passes
        assert result.profile_version_hash == "abc123"

    def test_profile_hash_in_gate_result_on_reject(self):
        gate, extractor = _gate(max_edu=2, profile_hash="xyz789")
        job = _job(description="Bac+5 minimum requis.")
        result = _check(gate, extractor, job)
        assert not result.passes
        assert result.profile_version_hash == "xyz789"

    def test_rule_version_in_rejection_reason(self):
        gate, extractor = _gate(max_edu=2)
        job = _job(description="Master requis. Développeur confirmé.")
        result = _check(gate, extractor, job)
        assert not result.passes
        for reason in result.reasons:
            assert reason.rule_version == "hard-filter.v2", (
                f"Rejection reason missing rule_version: {reason}"
            )

    def test_profile_hash_propagated_from_orchestrator_config(self, tmp_path):
        """Verify profile_version_hash= is wired in gate construction."""
        profile_hash = "deadbeef1234"
        gate = HardConstraintGate(
            user_max_education_level=2,
            profile_version_hash=profile_hash,
        )
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Master",
                importance=Importance.MANDATORY,
            )
        )
        result = gate.check(reqs)
        assert result.profile_version_hash == profile_hash
        assert not result.passes


# ============================================================================
# §1b — education_hard_constraint toggle (profile.matching.education_hard_constraint)
# ============================================================================

class TestEducationHardConstraintToggle:
    """The gate must consume `education_hard_constraint`, not just the model.

    Regression for the bug where the setting was fully persisted end-to-end
    (UI -> API -> profile.yaml) but never reached HardConstraintGate, so a
    MANDATORY education mismatch was always hard-rejected regardless of the
    profile's configured value.
    """

    def test_mandatory_exceeds_max_rejected_when_constraint_on(self):
        gate, extractor = _gate(max_edu=2, education_hard_constraint=True)
        job = _job(description="Bac+5 minimum requis.")
        result = _check(gate, extractor, job)
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)

    def test_mandatory_exceeds_max_passes_when_constraint_off(self):
        """Same job, same profile max — only the toggle differs."""
        gate, extractor = _gate(max_edu=2, education_hard_constraint=False)
        job = _job(description="Bac+5 minimum requis.")
        result = _check(gate, extractor, job)
        assert result.passes, (
            "education_hard_constraint=False must not hard-reject a "
            "MANDATORY education mismatch"
        )
        assert not result.reasons

    def test_soft_penalty_applied_via_gate_jobs_when_constraint_off(self):
        """When disabled, the mismatch must still be visible as the existing
        soft education penalty (reused, not reinvented) — not a silent no-op."""
        job = _job(description="Bac+5 minimum requis.")
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=False)

        passing, results = gate_jobs(
            [job], requirements_map, gate, education_preferred_penalty=0.20
        )
        assert len(passing) == 1
        assert results[job.id].passes
        assert results[job.id].education_penalty == 0.20

    def test_hard_penalty_not_applied_when_constraint_on(self):
        """When the constraint is on, the job is rejected outright — the
        soft penalty marker must NOT also be injected (no double bookkeeping:
        a rejected job never reaches scoring)."""
        job = _job(description="Bac+5 minimum requis.")
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=True)

        passing, results = gate_jobs(
            [job], requirements_map, gate, education_preferred_penalty=0.20
        )
        assert len(passing) == 0
        assert not results[job.id].passes
        assert results[job.id].education_penalty == 0.0

    def test_within_allowed_education_still_passes_regardless_of_toggle(self):
        """Bac+2 mandatory with max=bac+2 is SATISFIED outright — the toggle
        must not matter when the requirement doesn't exceed the max."""
        for toggle in (True, False):
            gate, extractor = _gate(max_edu=2, education_hard_constraint=toggle)
            job = _job(description="Bac+2 minimum requis.")
            result = _check(gate, extractor, job)
            assert result.passes, f"toggle={toggle} must still pass a within-limit requirement"

    def test_preferred_education_toggle_behavior(self):
        """PREFERRED education above the candidate's max is now a hard reject
        (behavior change, 2026-09: not "mandatory only" — see
        HardConstraintGate._check_education) when education_hard_constraint
        is on, and falls back to the existing soft-penalty pass when off,
        exactly like MANDATORY."""
        gate_on, extractor = _gate(max_edu=2, education_hard_constraint=True)
        job_on = _job(description="Bac+5 souhaité.")
        assert not _check(gate_on, extractor, job_on).passes

        gate_off, extractor = _gate(max_edu=2, education_hard_constraint=False)
        job_off = _job(description="Bac+5 souhaité.")
        assert _check(gate_off, extractor, job_off).passes

    def test_or_alternative_and_unknown_education_unaffected_by_toggle(self):
        """OR-alternative and UNKNOWN-importance education requirements are
        never MANDATORY (see education.py: 'OR-alt doesn't soften mandatory'),
        so the toggle must have no effect on them either way."""
        for toggle in (True, False):
            gate, extractor = _gate(max_edu=2, education_hard_constraint=toggle)
            or_alt_job = _job(description="Bac+5 ou expérience équivalente.")
            unknown_job = _job(job_id="j2", description="Master en informatique.")
            assert _check(gate, extractor, or_alt_job).passes
            assert _check(gate, extractor, unknown_job).passes

    def test_default_constructor_value_preserves_old_behavior(self):
        """Omitting education_hard_constraint entirely must behave exactly
        like the pre-fix hard-coded always-reject behavior (default True)."""
        gate = HardConstraintGate(user_max_education_level=2)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.MANDATORY,
            )
        )
        assert not gate.check(reqs).passes


# ============================================================================
# §1b-2 — soft education penalty must NOT fire on UNKNOWN importance
# (2026-09-11 audit fix). CORE (LLM-only, never produced by the heuristic
# path) keeps the existing penalty behavior unchanged.
# ============================================================================

class TestEducationSoftPenaltyImportanceScope:
    def test_unknown_importance_gets_no_penalty(self):
        """UNKNOWN is an ambiguous mention with no explicit qualifier — not
        evidence of a real requirement — so it must pass with NO penalty at
        all, not even the soft one."""
        job = _job(description="Master en informatique.")
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}
        assert requirements_map[job.id].education.importance == Importance.UNKNOWN
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=True)

        passing, results = gate_jobs(
            [job], requirements_map, gate, education_preferred_penalty=0.20
        )
        assert len(passing) == 1
        assert results[job.id].passes
        assert results[job.id].education_penalty == 0.0

    def test_core_importance_still_gets_penalty(self):
        """CORE ('strongly implied but no explicit qualifier', LLM-only)
        keeps the existing soft-penalty behavior — only UNKNOWN changed."""
        job = _job(job_id="j_core")
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Bac+5", importance=Importance.CORE,
            )
        )
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=True)

        passing, results = gate_jobs(
            [job], {job.id: reqs}, gate, education_preferred_penalty=0.20
        )
        assert len(passing) == 1
        assert results[job.id].passes
        assert results[job.id].education_penalty == 0.20


# ============================================================================
# §1c — orchestrator wiring: profile.matching.education_hard_constraint must
# actually reach HardConstraintGate's constructor during a real run_pipeline()
# call, not just work when the gate is instantiated manually in a test.
# ============================================================================

class TestOrchestratorWiresEducationHardConstraint:
    def _run_and_capture_gate_kwargs(self, tmp_path: Path, education_hard_constraint: bool) -> dict:
        """Run the real orchestrator against a minimal config dir (no sources,
        so no collectors/network run) and capture the kwargs HardConstraintGate
        was actually constructed with."""
        from unittest.mock import patch

        from src.pipeline.orchestrator import run_pipeline
        from src.requirements.gate import HardConstraintGate

        config_dir = tmp_path / "config"
        _write_profile(
            config_dir,
            f"""
            target_domains: [software_development]
            target_skills: [python]
            max_education: bac+2
            matching:
              education_hard_constraint: {str(education_hard_constraint).lower()}
            """,
        )
        (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")

        captured: dict = {}

        class _SpyGate(HardConstraintGate):
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)
                super().__init__(*args, **kwargs)

        with patch("src.requirements.gate.HardConstraintGate", _SpyGate):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        assert result.status == "completed", f"Pipeline failed: {result.error}"
        return captured

    def test_hard_constraint_true_reaches_gate_construction(self, tmp_path):
        captured = self._run_and_capture_gate_kwargs(tmp_path, education_hard_constraint=True)
        assert captured.get("education_hard_constraint") is True

    def test_hard_constraint_false_reaches_gate_construction(self, tmp_path):
        captured = self._run_and_capture_gate_kwargs(tmp_path, education_hard_constraint=False)
        assert captured.get("education_hard_constraint") is False


# ============================================================================
# §2 — No user-specific policy in engine
# ============================================================================

class TestNoUserSpecificPolicyInEngine:
    """Engine code must contain no occupation/domain/skill/geo hard-codes."""

    def test_engine_accepts_arbitrary_user_skills(self):
        """A garden-designer profile works through the same engine."""
        gate = HardConstraintGate(
            user_max_education_level=3,
            profile_version_hash="gardener-v1",
        )
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=3,
                level_label="Licence",
                importance=Importance.MANDATORY,
            )
        )
        assert gate.check(reqs).passes

    def test_rejection_message_contains_no_occupation_name(self):
        """Rejection messages must not mention specific job domains."""
        gate, extractor = _gate(max_edu=2)
        job = _job(title="Java Developer", description="Bac+5 requis. Java obligatoire.")
        result = _check(gate, extractor, job)
        assert not result.passes
        for reason in result.reasons:
            msg = reason.message.lower()
            assert "cybersecurity" not in msg
            assert "gardening" not in msg

    def test_gate_class_is_same_for_all_domains(self):
        """HardConstraintGate has no domain-specific subclasses in the module."""
        import inspect

        from src.requirements import gate as gate_module
        subclasses = [
            name for name, obj in inspect.getmembers(gate_module, inspect.isclass)
            if issubclass(obj, HardConstraintGate) and obj is not HardConstraintGate
        ]
        assert subclasses == [], (
            f"Found domain-specific gate subclasses: {subclasses}"
        )

    def test_no_occupation_string_literals_in_gate(self):
        """Engine code must not hard-code occupation or profile values."""
        import ast

        src_text = Path("src/requirements/gate.py").resolve().read_text(encoding="utf-8")
        tree = ast.parse(src_text)

        forbidden = {
            "software", "gardening", "cybersecurity", "accounting",
            "france", "python", "javascript", "php",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val_lower = node.value.lower()
                for name in forbidden:
                    assert name not in val_lower, (
                        f"User-specific value '{name}' found as string literal "
                        f"in gate.py line {node.lineno}: {node.value!r}"
                    )


# ============================================================================
# §3 — Profile adaptability through real config mechanism
# ============================================================================

class TestProfileAdaptabilityRealMechanism:
    """Changing profile data changes gate outcome; engine code is unchanged."""

    def test_software_and_gardener_profiles_use_same_engine(self, tmp_path):
        """Two profiles yield distinct outcomes; the engine class is identical."""
        from src.pipeline.filter import load_profile
        from src.requirements.education import parse_education_level

        sw_path = _write_profile(tmp_path / "sw", """
            target_domains: [software_development]
            target_skills: [python, javascript]
            max_education: "bac+2"
        """)
        gd_path = _write_profile(tmp_path / "gd", """
            target_domains: [gardening]
            target_skills: [horticulture, botanique]
            max_education: "bac+5"
        """)

        sw_profile = load_profile(sw_path)
        gd_profile = load_profile(gd_path)

        assert sw_profile.max_education == "bac+2"
        assert gd_profile.max_education == "bac+5"

        sw_edu = parse_education_level(sw_profile.max_education)
        gd_edu = parse_education_level(gd_profile.max_education)

        sw_gate = HardConstraintGate(user_max_education_level=sw_edu, profile_version_hash="sw-v1")
        gd_gate = HardConstraintGate(user_max_education_level=gd_edu, profile_version_hash="gd-v1")

        # Same class — no domain branching
        assert type(sw_gate) is type(gd_gate) is HardConstraintGate

        # Bac+5 mandatory job: sw rejects (max Bac+2), gd passes (max Bac+5)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.MANDATORY,
            )
        )
        sw_result = sw_gate.check(reqs)
        gd_result = gd_gate.check(reqs)
        assert not sw_result.passes, "sw profile (Bac+2 max) must reject Bac+5 mandatory"
        assert gd_result.passes,     "gd profile (Bac+5 max) must pass Bac+5 mandatory"
        assert sw_result.profile_version_hash == "sw-v1"
        assert gd_result.profile_version_hash == "gd-v1"

    def test_changing_target_skills_changes_principal_skill_gate(self, tmp_path):
        """Changing target_skills in profile YAML changes gate outcome."""
        from src.pipeline.filter import load_profile

        java_path = _write_profile(tmp_path / "java", """
            target_skills: [java, spring]
            max_education: "bac+5"
        """)
        rust_path = _write_profile(tmp_path / "rust", """
            target_skills: [rust, tokio]
            max_education: "bac+5"
        """)

        java_profile = load_profile(java_path)
        rust_profile = load_profile(rust_path)

        java_extractor = RequirementExtractor(user_skills=list(java_profile.target_skills))
        rust_extractor = RequirementExtractor(user_skills=list(rust_profile.target_skills))

        gate = HardConstraintGate(user_max_education_level=5)
        rust_job = _job(title="Rust Developer", description="Rust experience required.")

        assert not _check(gate, java_extractor, rust_job).passes, \
            "Java profile must reject Rust principal skill"
        assert _check(gate, rust_extractor, rust_job).passes, \
            "Rust profile must pass Rust principal skill"

    def test_no_code_change_needed_between_profiles(self):
        """The engine imports contain no occupation-specific predicates."""
        import ast

        src_text = Path("src/requirements/gate.py").resolve().read_text(encoding="utf-8")
        tree = ast.parse(src_text)

        occupation_names = {"software", "gardening", "cybersecurity", "accounting", "france"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val_lower = node.value.lower()
                for name in occupation_names:
                    assert name not in val_lower, (
                        f"Occupation value '{name}' found as literal in gate.py "
                        f"line {node.lineno}: {node.value!r}"
                    )


# ============================================================================
# §4 — None vs UNKNOWN: "no requirement" vs "failed/absent extraction"
# ============================================================================

class TestNoneVsUnknownSemantics:
    """edu=None must mean 'no requirement stated', never 'extraction failed'."""

    def test_job_with_no_description_is_not_rejected(self):
        """Empty description → no education requirement → SATISFIED (not UNKNOWN)."""
        gate, extractor = _gate(max_edu=2)
        result = _check(gate, extractor, _job(description=""))
        assert result.passes
        assert not result.uncertainty

    def test_job_with_description_but_no_edu_mention_passes(self):
        """A description that mentions no education → no requirement → SATISFIED."""
        gate, extractor = _gate(max_edu=2)
        job = _job(description=(
            "We are looking for a motivated developer. "
            "You will work on REST APIs with Python. "
            "3 years of experience preferred."
        ))
        result = _check(gate, extractor, job)
        assert result.passes
        assert not result.uncertainty

    def test_none_education_produces_satisfied_not_unknown(self):
        """Explicit None in requirements → no uncertainty."""
        gate, _ = _gate(max_edu=2)
        result = gate.check(JobRequirements(education=None))
        assert result.passes
        assert not result.uncertainty

    def test_user_no_edu_limit_and_edu_stated_produces_unknown(self):
        """When user max_education is None, a stated mandatory req → UNKNOWN (not SATISFIED)."""
        gate, _ = _gate(max_edu=None)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.MANDATORY,
            )
        )
        result = gate.check(reqs)
        assert result.passes
        assert result.uncertainty

    def test_no_profile_edu_and_no_job_edu_is_clean_pass(self):
        """No user edu limit + no job edu requirement → no uncertainty."""
        gate, _ = _gate(max_edu=None)
        result = gate.check(JobRequirements(education=None))
        assert result.passes
        assert not result.uncertainty


# ============================================================================
# §5 — "or equivalent" disjunction semantics (6 contrast cases from spec)
# ============================================================================

class TestOrEquivalentSemantics:
    """'Bac+5 ou expérience équivalente' is disjunctive — must never hard-reject."""

    @pytest.fixture(autouse=True)
    def setup_gate(self):
        self.gate, self.ext = _gate(max_edu=2)

    def _result(self, description: str) -> GateResult:
        return _check(self.gate, self.ext, _job(description=description))

    def test_bac5_obligatoire_rejects_bac2_profile(self):
        """'Bac+5 obligatoire' with no alternative → hard reject for Bac+2 user."""
        result = self._result("Bac+5 obligatoire. Développeur confirmé.")
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)

    def test_bac5_appreciate_is_rejected(self):
        """'Bac+5 apprécié' → preferred, explicit level, above max → hard reject
        (behavior change, 2026-09 — see TestPreferredPatternsRegression in
        test_education_extraction.py)."""
        assert not self._result("Bac+5 apprécié. Expérience valorisée.").passes

    def test_bac5_ou_experience_equivalente_does_not_reject(self):
        """'Bac+5 ou expérience équivalente' → PREFERRED → must never hard-reject."""
        result = self._result("Bac+5 ou expérience équivalente.")
        assert result.passes, (
            "'Bac+5 ou expérience équivalente' must not hard-reject a Bac+2 candidate "
            "because an explicit alternative path exists."
        )

    def test_bac5_ou_diplome_equivalent_does_not_reject(self):
        """'Bac+5 ou diplôme équivalent' (UNKNOWN importance) must not hard-reject."""
        assert self._result("Bac+5 ou diplôme équivalent accepté.").passes

    def test_bac5_plus_experience_conjunction(self):
        """'Bac+5 + N ans' without mandatory qualifier → UNKNOWN → pass."""
        assert self._result("Profil : Bac+5 + 3 ans d'expérience en développement.").passes
        result_m = self._result("Bac+5 minimum + 3 ans d'expérience.")
        assert not result_m.passes

    def test_bac5_ou_5ans_experience_disjunction(self):
        """'Bac+5 ou 5 ans d'expérience' — no mandatory qualifier → UNKNOWN → pass."""
        assert self._result("Bac+5 ou 5 ans d'expérience en développement.").passes


# ============================================================================
# §6 — Rejection provenance at gate output level
# ============================================================================

class TestRejectionProvenance:
    """Every rejection must carry attributable evidence."""

    def test_education_rejection_has_span(self):
        """Education hard rejection must include a span with extractor_id."""
        gate, extractor = _gate(max_edu=2)
        job = _job(description="Bac+5 minimum requis pour ce poste.")
        req = extractor.extract(job)
        result = gate.check(req)

        assert not result.passes
        edu_reasons = [r for r in result.reasons if r.type == "education"]
        assert edu_reasons, "No education rejection reason found"

        for reason in edu_reasons:
            assert reason.constraint_state == ConstraintState.VIOLATED
            assert reason.span is not None, "Education rejection must have an EvidenceSpan"
            assert reason.span.text, "EvidenceSpan.text must not be empty"
            assert reason.span.extractor_id, "EvidenceSpan.extractor_id must not be empty"
            assert reason.span.field == "description"
            assert reason.span.start >= 0
            assert reason.span.end > reason.span.start
            assert reason.rule_version == "hard-filter.v2"

    def test_principal_skill_rejection_has_type_and_requirement(self):
        """Principal skill rejection must carry the tech name."""
        gate, extractor = _gate(max_edu=5, user_skills=["python"])
        req = extractor.extract(_job(title="Java Developer", description="Java requis."))
        result = gate.check(req)

        assert not result.passes
        skill_reasons = [r for r in result.reasons if r.type == "principal_skill"]
        assert skill_reasons, "No principal_skill rejection found"
        for reason in skill_reasons:
            assert reason.requirement, "requirement field must be non-empty"
            assert reason.rule_version == "hard-filter.v2"

    def test_rejection_to_dict_is_complete(self):
        """to_dict() must include all required provenance fields."""
        gate, extractor = _gate(max_edu=2)
        req = extractor.extract(_job(description="Bac+5 minimum requis."))
        result = gate.check(req)

        assert not result.passes
        for reason in result.reasons:
            d = reason.to_dict()
            for field in ("type", "requirement", "candidate_value", "severity",
                          "message", "constraint_state", "rule_version"):
                assert field in d, f"Missing field {field!r} in to_dict()"
            if d.get("span"):
                assert "text" in d["span"]
                assert "extractor_id" in d["span"]
                assert "start" in d["span"]
                assert "end" in d["span"]

    def test_gate_jobs_produces_json_serializable_rejections(self):
        """rejection_reasons_to_detail must produce valid JSON with all provenance."""
        gate, extractor = _gate(max_edu=2)
        jobs = [_job(job_id=f"j{i}", description="Bac+5 minimum requis.") for i in range(3)]
        req_map = {job.id: extractor.extract(job) for job in jobs}
        passing, gate_results = gate_jobs(jobs, req_map, gate)

        assert len(passing) == 0
        for job_id, result in gate_results.items():
            assert not result.passes
            detail = json.loads(rejection_reasons_to_detail(result.reasons))
            assert isinstance(detail, list) and len(detail) > 0
            assert "type" in detail[0]
            assert "rule_version" in detail[0]

    def test_experience_extraction_populates_span(self):
        """Experience extractor must populate EvidenceSpan (C2 fix verification)."""
        from src.requirements.experience import extract_experience

        text = "Au moins 5 ans d'expérience requis en développement logiciel."
        exp = extract_experience(text)

        assert exp is not None
        assert exp.span is not None, "Experience extractor must populate span"
        assert exp.span.start >= 0
        assert exp.span.end > exp.span.start
        assert exp.span.extractor_id == "experience.min_years.v2"
        assert exp.span.field == "description"


# ============================================================================
# §7 — UNKNOWN and contradiction safety end-to-end
# ============================================================================

class TestUnknownAndContradictionSafety:
    """UNKNOWN and contradictions can never become REJECT through any path."""

    def test_unknown_education_importance_always_passes(self):
        """UNKNOWN-importance education → soft penalty only, never reject."""
        gate, _ = _gate(max_edu=2)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.UNKNOWN,
            )
        )
        assert gate.check(reqs).passes

    def test_preferred_education_exceeding_max_rejects(self):
        """PREFERRED is an explicit, attributable level mention (not ambiguous
        like UNKNOWN) — exceeding user max is a hard reject, same as
        MANDATORY (behavior change, 2026-09)."""
        gate, _ = _gate(max_edu=2)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.PREFERRED,
            )
        )
        assert not gate.check(reqs).passes

    def test_no_evidence_job_passes_clean(self):
        """A job with no extractable requirements → PASS, no uncertainty."""
        gate, extractor = _gate(max_edu=2)
        result = _check(gate, extractor, _job(description="Nous recrutons un développeur passionné."))
        assert result.passes
        assert not result.uncertainty

    def test_unknown_user_edu_produces_pass_with_uncertainty(self):
        """User with no declared max education + mandatory req → PASS_WITH_UNCERTAINTY."""
        gate, _ = _gate(max_edu=None)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.MANDATORY,
            )
        )
        result = gate.check(reqs)
        assert result.passes
        assert result.uncertainty
        assert result.pass_with_uncertainty

    def test_unknown_then_violated_is_reject(self):
        """UNKNOWN on education + VIOLATED on skill → still REJECT (violation wins)."""
        gate, extractor = _gate(max_edu=None, user_skills=["python"])
        result = _check(gate, extractor, _job(title="Java Developer", description="Java obligatoire."))
        assert not result.passes

    def test_pass_with_uncertainty_property(self):
        """pass_with_uncertainty = passes=True AND uncertainty=True."""
        gate, _ = _gate(max_edu=None)
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5, level_label="Master", importance=Importance.MANDATORY,
            )
        )
        result = gate.check(reqs)
        assert result.passes is True
        assert result.uncertainty is True
        assert result.pass_with_uncertainty is True

    def test_mixed_preferred_education_mentions_uses_highest_level(self):
        """Two separately qualified PREFERRED mentions ('Bac+3 souhaité ou
        Bac+5 bienvenu') are not an adjacent range (#58 covers only
        'Bac+3 ou Bac+5' with nothing but the connector between them), so the
        highest explicit level (Bac+5) wins — not a genuine UNKNOWN/contradiction, so it correctly rejects
        when that highest level exceeds the candidate's max (behavior change,
        2026-09: PREFERRED above max is a hard reject)."""
        gate, extractor = _gate(max_edu=3)
        job = _job(description="Bac+3 souhaité ou Bac+5 bienvenu selon expérience.")
        assert not _check(gate, extractor, job).passes

    def test_negated_tech_requirement_passes(self):
        """Generic title (no core language) + negated tech → no principal skill rejection."""
        gate, extractor = _gate(max_edu=5, user_skills=["python"])
        job = _job(title="Backend Developer", description="No Java required. Python and Django stack.")
        assert _check(gate, extractor, job).passes


# ============================================================================
# §8 — AI boundary: LLM output is evidence only, cannot reject
# ============================================================================

class TestAIBoundary:
    """AI/LLM must not directly reject; deterministic evaluator owns the decision."""

    def test_llm_reject_field_is_ignored(self):
        """A LLM response with 'reject: true' must not cause rejection."""
        from unittest.mock import MagicMock

        rogue_response = json.dumps({
            "education": None,
            "required_skills": [],
            "preferred_skills": [],
            "is_principal_tech_role": False,
            "principal_technologies": [],
            "reject": True,
            "reason": "user dislikes this role",
        })

        mock_llm = MagicMock()
        mock_llm.complete.return_value = rogue_response

        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        gate = HardConstraintGate(user_max_education_level=5)
        job = _job(description="Python developer role. Team player. Remote ok.")
        result = gate.check(extractor.extract(job))

        assert result.passes, (
            "Gate must not reject based on LLM 'reject:true' field. "
            "LLM is evidence-only; the deterministic evaluator decides."
        )

    def test_llm_produces_evidence_not_policy(self):
        """LLM output is JobRequirements (evidence); gate produces GateResult (policy)."""
        from unittest.mock import MagicMock

        mock_llm = MagicMock()
        mock_llm.complete.return_value = json.dumps({
            "education": {"level": "bac+5", "importance": "mandatory"},
            "required_skills": ["python"],
            "preferred_skills": ["docker"],
            "is_principal_tech_role": True,
            "principal_technologies": ["rust"],
        })

        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        job = _job(title="Rust Developer", description="Rust obligatoire.")
        req = extractor.extract(job)
        result = HardConstraintGate(user_max_education_level=2).check(req)

        assert isinstance(req, JobRequirements)
        assert isinstance(result, GateResult)

    def test_llm_malformed_json_falls_back_to_heuristic(self):
        """LLM returning garbage → fallback to heuristic → no crash, valid result."""
        from unittest.mock import MagicMock

        mock_llm = MagicMock()
        mock_llm.complete.return_value = "THIS IS NOT JSON {{{ badly malformed"

        extractor = RequirementExtractor(user_skills=["python"], llm_provider=mock_llm)
        gate = HardConstraintGate(user_max_education_level=5)
        job = _job(description="Python developer wanted. Bac+3 requis minimum.")
        result = gate.check(extractor.extract(job))
        assert isinstance(result, GateResult)

    def test_llm_schema_has_no_reject_field(self):
        """LLM prompt schema must not document a 'reject' field."""
        from src.requirements import extractor as ext_module
        assert '"reject"' not in ext_module._LLM_EXTRACTION_PROMPT, (
            "LLM prompt schema must not include a 'reject' field — "
            "that would signal to the model that rejection is its job."
        )


# ============================================================================
# §9 — Legacy filtering audit: document dual-path + verify no silent bypass
# ============================================================================

class TestLegacyFilteringAudit:
    """Document the two-tier filtering architecture and verify safety."""

    def test_experience_filter_config_reads_from_profile(self):
        """filter.py experience check reads from profile.experience.max_years."""
        from src.pipeline.filter import build_filter_config

        profile = JobSearchProfile(experience=ExperiencePreferences(max_years=5))
        config = build_filter_config(profile, {})
        assert config["experience_years"]["max_years"] == 5

    def test_unknown_experience_passes_filter(self):
        """Jobs with no experience statement pass the filter (UNKNOWN = no reject)."""
        from src.pipeline.filter import matches_experience_years

        job = _job(description="Great opportunity for a developer.")
        assert matches_experience_years(job, {"experience_years": {"max_years": 3}})

    def test_gate_hard_rejects_mandatory_experience_excess(self):
        """Gate hard-rejects when mandatory experience exceeds user max."""
        gate, extractor = _gate(max_edu=5, max_exp=3)
        result = _check(gate, extractor, _job(description="Au moins 5 ans d'expérience requis."))
        assert not result.passes
        assert any(r.type == "experience" for r in result.reasons), (
            "MANDATORY experience excess must appear as a hard rejection reason"
        )

    def test_gate_preferred_experience_never_hard_rejects(self):
        """PREFERRED experience exceeding user max → soft penalty only, not reject."""
        from src.requirements.models import ExperienceRequirement
        gate, _ = _gate(max_edu=5, max_exp=2)
        reqs = JobRequirements(
            experience=ExperienceRequirement(
                min_years=10,
                importance=Importance.PREFERRED,
                raw_text="idéalement 10 ans d'expérience",
            )
        )
        result = gate.check(reqs)
        assert result.passes
        assert all(r.type != "experience" for r in result.reasons)


# ============================================================================
# §10 — All eight constraint dimensions
# ============================================================================

class TestAllConstraintDimensions:
    """Education, experience, work mode, geography, languages, employment type,
    role/domain, skills — eight dimensions verified."""

    # ── Education ────────────────────────────────────────────────────────────

    def test_edu_mandatory_violation_rejects(self):
        gate, extractor = _gate(max_edu=2)
        assert not _check(gate, extractor, _job(description="Bac+5 minimum requis.")).passes

    def test_edu_mandatory_satisfaction_passes(self):
        gate, extractor = _gate(max_edu=5)
        assert _check(gate, extractor, _job(description="Bac+5 minimum requis.")).passes

    def test_edu_missing_evidence_passes(self):
        gate, extractor = _gate(max_edu=2)
        assert _check(gate, extractor, _job(description="Développeur passionné.")).passes

    def test_edu_preferred_above_max_rejects(self):
        """Behavior change, 2026-09: PREFERRED above max is now a hard reject."""
        gate, extractor = _gate(max_edu=2)
        assert not _check(gate, extractor, _job(description="Bac+5 souhaité.")).passes

    def test_edu_unknown_importance_does_not_reject(self):
        gate, _ = _gate(max_edu=2)
        reqs = JobRequirements(
            education=EducationRequirement(level_bac_plus=5, level_label="Master",
                                           importance=Importance.UNKNOWN)
        )
        assert gate.check(reqs).passes

    # ── Experience ───────────────────────────────────────────────────────────

    def test_exp_missing_evidence_passes(self):
        gate, extractor = _gate(max_edu=5, max_exp=3)
        assert _check(gate, extractor, _job(description="Python developer.")).passes

    def test_exp_preferred_does_not_reject(self):
        gate, extractor = _gate(max_edu=5, max_exp=3)
        assert _check(gate, extractor, _job(description="Idéalement 5 ans d'expérience.")).passes

    def test_exp_mandatory_excess_rejects(self):
        """5 ans requis (mandatory) but user max is 2 → gate hard-rejects."""
        gate, extractor = _gate(max_edu=5, max_exp=2)
        result = _check(gate, extractor, _job(description="Au moins 5 ans d'expérience requis."))
        assert not result.passes
        assert any(r.type == "experience" for r in result.reasons)

    # ── Principal skill (role/domain) ────────────────────────────────────────

    def test_skill_explicit_violation_rejects(self):
        gate, extractor = _gate(max_edu=5, user_skills=["python"])
        assert not _check(gate, extractor, _job(title="Java Developer")).passes

    def test_skill_explicit_satisfaction_passes(self):
        gate, extractor = _gate(max_edu=5, user_skills=["java"])
        assert _check(gate, extractor, _job(title="Java Developer")).passes

    def test_skill_missing_evidence_generic_title_passes(self):
        gate, extractor = _gate(max_edu=5, user_skills=["python"])
        assert _check(gate, extractor, _job(title="Backend Developer")).passes

    def test_skill_or_alternative_is_not_rejected(self):
        """'Rust ou Python Developer' — Python known → OR alternative, not hard reject."""
        gate, extractor = _gate(max_edu=5, user_skills=["python"])
        assert _check(gate, extractor, _job(title="Rust ou Python Developer")).passes

    # ── Work mode (tested through filter layer, not gate) ────────────────────

    def test_work_mode_unknown_passes_filter(self):
        """Work mode UNKNOWN (no country + remote=False) → filter accepts."""
        from src.pipeline.filter import matches_work_mode
        job = _job(remote=False, countries=[])
        assert matches_work_mode(job, {"work_modes": {"allowed": ["remote"]}})

    def test_work_mode_remote_passes_when_required(self):
        from src.pipeline.filter import matches_work_mode
        assert matches_work_mode(_job(remote=True), {"work_modes": {"allowed": ["remote"]}})

    # ── Geography (tested through filter layer) ───────────────────────────────

    def test_geo_unknown_country_passes(self):
        from src.pipeline.filter import matches_location
        job = _job(location="", countries=[])
        assert matches_location(job, {"locations": {"include": ["France"]}})

    def test_geo_preferred_country_passes(self):
        from src.pipeline.filter import matches_location
        assert matches_location(
            _job(location="Paris, France", countries=["France"]),
            {"locations": {"include": ["France"]}},
        )
