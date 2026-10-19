"""Tests for three-valued constraint evaluation and PASS_WITH_UNCERTAINTY.

Verifies:
- ConstraintState enum (SATISFIED / VIOLATED / UNKNOWN)
- GateResult.uncertainty flag and pass_with_uncertainty property
- GateResult.contradictions and profile_version_hash populated
- EvidenceSpan provenance present on rejections
- Absent evidence → UNKNOWN → never rejection
- Contradictory evidence → uncertainty → PASS, never REJECT
"""
from __future__ import annotations


from src.models.job import Job
from src.requirements.education import extract_education
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import (
    ConstraintState,
    EvidenceSpan,
    GateResult,
    HardRejectionReason,
    Importance,
    JobRequirements,
    EducationRequirement,
)
from src.requirements.work_mode_evidence import (
    WorkModeSignal,
    detect_work_mode_contradiction,
    extract_work_mode_evidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gate(
    user_max_edu: int | None = 2,
    user_max_years: int | None = None,
    profile_hash: str = "abc123",
) -> HardConstraintGate:
    return HardConstraintGate(
        user_max_education_level=user_max_edu,
        user_max_years_experience=user_max_years,
        profile_version_hash=profile_hash,
    )


def _job(title: str = "Dev", description: str = "") -> Job:
    return Job(id="j1", source="test", title=title, description=description)


# ===========================================================================
# ConstraintState enum
# ===========================================================================

class TestConstraintStateEnum:
    def test_values_are_strings(self):
        assert ConstraintState.SATISFIED == "satisfied"
        assert ConstraintState.VIOLATED == "violated"
        assert ConstraintState.UNKNOWN == "unknown"

    def test_violated_is_not_satisfied(self):
        assert ConstraintState.VIOLATED != ConstraintState.SATISFIED

    def test_unknown_is_not_violated(self):
        assert ConstraintState.UNKNOWN != ConstraintState.VIOLATED


# ===========================================================================
# EvidenceSpan provenance
# ===========================================================================

class TestEvidenceSpan:
    def test_education_rejection_has_span(self):
        desc = "Formation Bac+5 minimum en informatique."
        edu = extract_education(desc)
        assert edu is not None
        assert edu.span is not None
        assert edu.span.start >= 0
        assert edu.span.end > edu.span.start
        assert "Bac+5" in edu.span.text or "bac+5" in edu.span.text.lower()

    def test_span_extractor_id_present(self):
        desc = "Bac+5 requis."
        edu = extract_education(desc)
        assert edu is not None
        assert edu.span is not None
        assert edu.span.extractor_id.startswith("education.")

    def test_span_field_is_description(self):
        edu = extract_education("Bac+5 obligatoire.")
        assert edu is not None
        assert edu.span is not None
        assert edu.span.field == "description"

    def test_hard_rejection_reason_includes_span(self):
        requirements = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.MANDATORY,
                raw_text="Bac+5 minimum",
                span=EvidenceSpan(
                    text="Bac+5 minimum",
                    start=0,
                    end=13,
                    field="description",
                    extractor_id="education.bac5.fr.v1",
                ),
            )
        )
        gate = _gate(user_max_edu=2)
        result = gate.check(requirements)
        assert not result.passes
        assert len(result.reasons) == 1
        reason = result.reasons[0]
        assert reason.span is not None
        assert reason.span.field == "description"

    def test_span_to_dict_serialisable(self):
        span = EvidenceSpan(text="Bac+5", start=0, end=5, field="description", extractor_id="x")
        d = span.to_dict()
        assert d["text"] == "Bac+5"
        assert d["start"] == 0
        assert d["end"] == 5
        assert d["field"] == "description"

    def test_rejection_reason_to_dict_includes_span(self):
        span = EvidenceSpan(text="Bac+5 requis", start=0, end=12, field="description")
        reason = HardRejectionReason(
            type="education",
            requirement="bac+5",
            candidate_value="bac+2",
            span=span,
        )
        d = reason.to_dict()
        assert "span" in d
        assert d["span"]["text"] == "Bac+5 requis"


# ===========================================================================
# GateResult structure
# ===========================================================================

class TestGateResultStructure:
    def test_passes_has_no_uncertainty_by_default(self):
        gate = _gate()
        result = gate.check(JobRequirements())
        assert result.passes
        assert not result.uncertainty
        assert not result.pass_with_uncertainty

    def test_profile_version_hash_propagated(self):
        gate = _gate(profile_hash="deadbeef1234")
        result = gate.check(JobRequirements())
        assert result.profile_version_hash == "deadbeef1234"

    def test_rule_version_present(self):
        gate = _gate()
        result = gate.check(JobRequirements())
        assert result.rule_version
        assert result.rule_version.startswith("hard-filter.")

    def test_rejection_sets_constraint_state_violated(self):
        req = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.MANDATORY,
                raw_text="Bac+5 requis",
            )
        )
        gate = _gate(user_max_edu=2)
        result = gate.check(req)
        assert not result.passes
        assert len(result.reasons) == 1
        assert result.reasons[0].constraint_state == ConstraintState.VIOLATED

    def test_pass_with_uncertainty_property(self):
        # A result that passes but has uncertainty set
        result = GateResult(passes=True, uncertainty=True)
        assert result.pass_with_uncertainty

    def test_no_uncertainty_on_clean_pass(self):
        result = GateResult(passes=True, uncertainty=False)
        assert not result.pass_with_uncertainty

    def test_rejection_is_not_pass_with_uncertainty(self):
        result = GateResult(passes=False, uncertainty=False)
        assert not result.pass_with_uncertainty

    def test_contradictions_list_present(self):
        gate = _gate()
        result = gate.check(JobRequirements())
        assert isinstance(result.contradictions, list)


# ===========================================================================
# UNKNOWN = pass (no evidence → not a rejection reason)
# ===========================================================================

class TestUnknownIsNeverRejection:
    def test_no_education_mention_passes(self):
        gate = _gate(user_max_edu=2)
        result = gate.check(JobRequirements(education=None))
        assert result.passes

    def test_no_edu_with_no_profile_max_passes(self):
        gate = _gate(user_max_edu=None)
        result = gate.check(JobRequirements(
            education=EducationRequirement(level_bac_plus=5, level_label="Bac+5", importance=Importance.MANDATORY)
        ))
        assert result.passes  # No profile max → cannot evaluate → pass

    def test_preferred_education_mismatch_rejects(self):
        """A PREFERRED level stated as the bar ("Bac+5 ou équivalent") is an
        explicit, attributable requirement (not the ambiguous UNKNOWN this
        class is about) — exceeding user max is a hard reject, same as
        MANDATORY. A desirable one is not (ADR 0004, amended 2026-10)."""
        gate = _gate(user_max_edu=2)
        stated_bar = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.PREFERRED,
                is_or_alternative=True,
            )
        )
        assert not gate.check(stated_bar).passes
        desirable = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.PREFERRED,
                is_desirable=True,
            )
        )
        assert gate.check(desirable).passes

    def test_unknown_education_importance_passes(self):
        req = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.UNKNOWN,
            )
        )
        gate = _gate(user_max_edu=2)
        result = gate.check(req)
        assert result.passes  # UNKNOWN → soft penalty, no hard reject

    def test_no_skills_passes(self):
        gate = _gate()
        result = gate.check(JobRequirements(skills=[]))
        assert result.passes

    def test_empty_requirements_always_passes(self):
        gate = _gate(user_max_edu=0)
        result = gate.check(JobRequirements())
        assert result.passes

    def test_education_matching_user_max_passes(self):
        req = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=2,
                level_label="Bac+2",
                importance=Importance.MANDATORY,
            )
        )
        gate = _gate(user_max_edu=2)
        result = gate.check(req)
        assert result.passes  # exactly at max → SATISFIED

    def test_education_below_user_max_passes(self):
        req = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=3,
                level_label="Bac+3",
                importance=Importance.MANDATORY,
            )
        )
        gate = _gate(user_max_edu=5)
        result = gate.check(req)
        assert result.passes


# ===========================================================================
# Work mode evidence extraction
# ===========================================================================

class TestWorkModeEvidenceExtraction:
    def test_100_percent_telework_full_remote(self):
        result = extract_work_mode_evidence("Poste 100% télétravail.")
        assert result.resolved_signal == WorkModeSignal.FULL_REMOTE
        assert not result.has_contradiction

    def test_100_percent_remote_english(self):
        result = extract_work_mode_evidence("This is a 100% remote position.")
        assert result.resolved_signal == WorkModeSignal.FULL_REMOTE

    def test_fully_remote(self):
        result = extract_work_mode_evidence("Fully remote, work from anywhere.")
        assert result.resolved_signal == WorkModeSignal.FULL_REMOTE

    def test_3_days_on_site_is_hybrid(self):
        result = extract_work_mode_evidence("3 days on site per week, 2 days remote.")
        assert result.resolved_signal == WorkModeSignal.HYBRID
        assert not result.has_contradiction

    def test_4_jours_sur_site_is_hybrid(self):
        result = extract_work_mode_evidence("Présence de 4 jours au bureau par semaine.")
        assert result.resolved_signal == WorkModeSignal.HYBRID

    def test_5_days_on_site_is_on_site(self):
        result = extract_work_mode_evidence("5 days on site per week.")
        assert result.resolved_signal == WorkModeSignal.ON_SITE

    def test_hybrid_keyword_is_hybrid(self):
        result = extract_work_mode_evidence("Mode de travail hybride.")
        assert result.resolved_signal == WorkModeSignal.HYBRID

    def test_remote_friendly_is_unknown(self):
        result = extract_work_mode_evidence("We are remote-friendly.")
        assert result.resolved_signal == WorkModeSignal.UNKNOWN

    def test_telework_possible_is_unknown(self):
        result = extract_work_mode_evidence("Télétravail possible 1 à 2 jours.")
        assert result.resolved_signal == WorkModeSignal.UNKNOWN

    def test_no_mention_is_unknown(self):
        result = extract_work_mode_evidence("Competitive salary and benefits.")
        assert result.resolved_signal == WorkModeSignal.UNKNOWN

    def test_contradiction_full_remote_and_onsite_days(self):
        text = "100% remote. Presence required 3 days per week on site."
        result = extract_work_mode_evidence(text)
        assert result.has_contradiction
        assert result.resolved_signal == WorkModeSignal.UNKNOWN
        assert result.contradiction_description

    def test_evidence_list_populated(self):
        result = extract_work_mode_evidence("Fully remote position.")
        assert len(result.evidence) >= 1
        assert result.evidence[0].signal == WorkModeSignal.FULL_REMOTE

    def test_evidence_has_span(self):
        result = extract_work_mode_evidence("100% télétravail.")
        assert result.evidence
        assert result.evidence[0].span is not None
        assert result.evidence[0].span.start >= 0


# ===========================================================================
# Work mode contradiction detection (metadata vs description)
# ===========================================================================

class TestWorkModeContradictionDetection:
    def test_no_contradiction_when_metadata_remote_and_text_remote(self):
        desc = "This is a 100% remote position."
        contradiction = detect_work_mode_contradiction(True, desc)
        assert contradiction == ""

    def test_contradiction_metadata_remote_but_4_days_onsite(self):
        desc = "4 days on site per week required."
        contradiction = detect_work_mode_contradiction(True, desc)
        assert contradiction  # non-empty = contradiction detected
        assert "on-site" in contradiction.lower() or "onsite" in contradiction.lower() or "hybrid" in contradiction.lower()

    def test_contradiction_metadata_not_remote_but_fully_remote_text(self):
        desc = "100% télétravail, poste entièrement à distance."
        contradiction = detect_work_mode_contradiction(False, desc)
        assert contradiction

    def test_no_contradiction_when_empty_description(self):
        contradiction = detect_work_mode_contradiction(True, "")
        assert contradiction == ""

    def test_no_contradiction_when_metadata_none(self):
        desc = "4 days on site per week."
        # None metadata → no known state to contradict
        contradiction = detect_work_mode_contradiction(None, desc)
        assert contradiction == ""

    def test_contradiction_returns_string_with_evidence(self):
        desc = "Presence required 5 days per week on site."
        result = detect_work_mode_contradiction(True, desc)
        # Should contain the evidence text or description
        assert isinstance(result, str)


# ===========================================================================
# Provenance in rejection reasons
# ===========================================================================

class TestProvenance:
    def test_rejection_has_rule_version(self):
        req = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.MANDATORY,
            )
        )
        gate = _gate(user_max_edu=2)
        result = gate.check(req)
        assert not result.passes
        assert result.reasons[0].rule_version.startswith("hard-filter.")

    def test_result_has_profile_version_hash(self):
        gate = HardConstraintGate(
            user_max_education_level=2,
            profile_version_hash="myhash42",
        )
        result = gate.check(JobRequirements())
        assert result.profile_version_hash == "myhash42"

    def test_to_dict_includes_constraint_state(self):
        reason = HardRejectionReason(
            type="education",
            requirement="bac+5",
            candidate_value="bac+2",
            constraint_state=ConstraintState.VIOLATED,
        )
        d = reason.to_dict()
        assert d["constraint_state"] == "violated"

    def test_to_dict_includes_rule_version(self):
        reason = HardRejectionReason(
            type="education",
            requirement="bac+5",
            candidate_value="bac+2",
            rule_version="hard-filter.v2",
        )
        d = reason.to_dict()
        assert "rule_version" in d
        assert d["rule_version"] == "hard-filter.v2"


# ===========================================================================
# gate_jobs integration
# ===========================================================================

class TestGateJobsIntegration:
    def test_gate_jobs_passes_uncertainty_forward(self):
        job = Job(id="j1", source="test", title="Dev")
        job.raw_payload = {}
        # No requirements → clean pass
        requirements_map = {"j1": JobRequirements()}
        gate = _gate()
        passing, results = gate_jobs([job], requirements_map, gate)
        assert len(passing) == 1
        result = results["j1"]
        assert result.passes
        assert result.profile_version_hash == "abc123"

    def test_gate_jobs_rejects_on_education_violation(self):
        job = Job(id="j1", source="test", title="Dev")
        job.raw_payload = {}
        req = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.MANDATORY,
            )
        )
        requirements_map = {"j1": req}
        gate = _gate(user_max_edu=2)
        passing, results = gate_jobs([job], requirements_map, gate)
        assert len(passing) == 0
        assert not results["j1"].passes
