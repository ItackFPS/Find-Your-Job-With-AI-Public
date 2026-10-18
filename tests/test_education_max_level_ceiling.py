"""Regression tests for the education hard-rejection ceiling being fully
driven by the existing, single `profile.max_education` setting (the same
field the frontend's "Maximum education level (Bac+N)" dropdown edits) —
NOT a second, hard-coded, or new configuration mechanism.

Behavior (see HardConstraintGate._check_education):
    max_education = None ("No limit") → no education hard rejection at all
    max_education = "bac+N"           → any MANDATORY or PREFERRED education
                                         requirement above bac+N is a hard
                                         reject (a held range counts at its
                                         lowest level, #58); UNKNOWN/ambiguous mentions
                                         and explicit experience-substitution
                                         alternatives remain exempt.

The real "STAGE DEVELOPPEUR WEB H/F" (Bezannes, ANETSYS) job — "Étudiant(e) en
Bac +3 à Bac + 4" — is a CURRENT ENROLMENT requirement, not a held degree
(issue #40, see TestExactBezannesJob).
"""
from __future__ import annotations

import pytest

from src.models.job import Job
from src.requirements.education import parse_education_level
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs


def _job(job_id: str = "j1", title: str = "Dev", description: str = "") -> Job:
    return Job(id=job_id, source="test", title=title, description=description)


def _check(description: str, max_education: str | None, title: str = "Dev"):
    """End-to-end: profile.max_education string -> parse -> real extractor -> real gate.

    Mirrors exactly what src/pipeline/orchestrator.py does:
        if profile.max_education: user_max_edu_level = parse_education_level(...)
    (both None and "" stay None -- the orchestrator's own truthiness guard).
    """
    user_max_edu = parse_education_level(max_education) if max_education else None
    job = _job(title=title, description=description)
    extractor = RequirementExtractor(user_skills=["python"])
    reqs = extractor.extract(job)
    gate = HardConstraintGate(user_max_education_level=user_max_edu)
    return gate.check(reqs)


class TestConfigurableCeilingDrivenByMaxEducation:
    """The SAME max_education value used elsewhere in the pipeline is the
    sole source of truth for the hard ceiling -- no second setting."""

    def test_max_bac2_bac3_requirement_rejected(self):
        result = _check("Bac+3 requis.", max_education="bac+2")
        assert not result.passes

    def test_max_bac3_bac3_requirement_accepted(self):
        result = _check("Bac+3 requis.", max_education="bac+3")
        assert result.passes

    def test_max_bac3_bac4_requirement_rejected(self):
        result = _check("Bac+4 requis.", max_education="bac+3")
        assert not result.passes

    def test_max_bac3_range_bac3_to_bac4_accepted(self):
        """A held range counts at its lowest level (Bac+3, #58), within max Bac+3."""
        result = _check("Formation Bac+3 à Bac+4 souhaitée.", max_education="bac+3")
        assert result.passes

    def test_max_bac3_range_bac4_to_bac5_rejected(self):
        """A range whose lowest level (Bac+4) exceeds max Bac+3 still rejects."""
        result = _check("Formation Bac+4 à Bac+5 souhaitée.", max_education="bac+3")
        assert not result.passes

    def test_max_no_limit_bac4_not_rejected_by_education(self):
        """max_education=None ('No limit' in the frontend) -> education never
        hard-rejects, regardless of the level mentioned."""
        result = _check("Bac+8 obligatoire.", max_education=None)
        assert result.passes

    def test_max_bac2_range_bac2_to_bac3_accepted(self):
        """The candidate holds the range's lower bound (#58)."""
        result = _check("Formation Bac+2 à Bac+3 souhaitée.", max_education="bac+2")
        assert result.passes

    def test_max_bac1_range_bac2_to_bac3_rejected(self):
        result = _check("Formation Bac+2 à Bac+3 souhaitée.", max_education="bac+1")
        assert not result.passes

    def test_max_bac3_range_bac2_to_bac3_accepted(self):
        """Same range text, higher configured max -> within ceiling."""
        result = _check("Formation Bac+2 à Bac+3 souhaitée.", max_education="bac+3")
        assert result.passes

    def test_same_requirement_different_ceilings(self):
        """The exact same job text must behave differently purely based on
        the configured max_education value -- proving the ceiling is
        genuinely read from configuration, not hard-coded."""
        desc = "Bac+4 requis en informatique."
        assert not _check(desc, max_education="bac+2").passes
        assert not _check(desc, max_education="bac+3").passes
        assert _check(desc, max_education="bac+4").passes
        assert _check(desc, max_education=None).passes


class TestExactBezannesJob:
    """The real production job 'STAGE DEVELOPPEUR WEB H/F' (ANETSYS,
    Bezannes/dept 51): 'Étudiant(e) en Bac +3 à Bac + 4'.

    Issue #40 (semantics confirmed by the owner, 2026-09-17): this targets
    students CURRENTLY ENROLLED in Bac+3 or Bac+4, and profile.max_education
    is the highest level HELD. A range starts at its lowest level, and being
    in year 3 requires having completed Bac+2 — so held Bac+2 and Bac+3
    candidates are eligible; only a candidate who cannot be in Bac+3 yet
    (held Bac+1) is rejected.

    These tests previously expected a rejection for max_education=bac+3.
    That outcome came from an unrelated 'ou équivalent' (qualifying
    'spécialité IA') leaking into the education reading, not from any
    representation of enrolment — without that phrase the same posting was
    accepted at every level.
    """

    DESCRIPTION = (
        "Présentation de l'entreprise\n\n"
        "Basée à Reims, ANETSYS est une entreprise de conseil et d'ingénierie informatique "
        "spécialisée dans la cybersécurité.\n\n"
        "Dans le cadre du développement de solutions innovantes intégrant l'Intelligence "
        "Artificielle, nous recherchons un(e) stagiaire Développeur pour participer à la "
        "conception et à l'amélioration d'applications exploitant les technologies de "
        "traitement de données, d'automatisation et d'analyse intelligente.\n\n"
        "Profil recherché\n\n"
        "Étudiant(e) en Bac +3 à Bac + 4, développement web, Data science, informatique, "
        "avec un spécialité IA ou équivalent.\n\n"
        "Connaissances de base en HTML, CSS, PHP et JavaScript.\n\n"
        "Bonne maîtrise de Python."
    )

    def _extract(self):
        job = _job(job_id="bezannes", title="STAGE DEVELOPPEUR WEB H/F", description=self.DESCRIPTION)
        extractor = RequirementExtractor(user_skills=["python", "javascript", "php", "html", "css"])
        return job, extractor.extract(job)

    def test_education_range_extracted_correctly(self):
        _, reqs = self._extract()
        assert reqs.education is not None
        assert reqs.education.level_bac_plus == 4, (
            "Range 'Bac+3 à Bac+4' must resolve to its highest level, Bac+4"
        )

    def test_unrelated_ou_equivalent_does_not_corrupt_level_extraction(self):
        """The 'ou équivalent' qualifying 'spécialité IA' later in the same
        sentence must not change the extracted LEVEL (still Bac+4) -- it may
        (and does, pre-existing behavior) affect the importance
        classification, but the level itself must remain correct."""
        _, reqs = self._extract()
        assert reqs.education.level_bac_plus == 4

    def test_is_an_enrolment_requirement_starting_at_bac3(self):
        from src.requirements.models import EducationKind

        _, reqs = self._extract()
        assert reqs.education.kind == EducationKind.ENROLLED
        assert reqs.education.enrolment_level_min == 3

    @pytest.mark.parametrize("held", ["bac+2", "bac+3"])
    def test_candidates_who_can_be_enrolled_in_bac3_pass(self, held):
        job, reqs = self._extract()
        gate = HardConstraintGate(user_max_education_level=parse_education_level(held))
        passing, results = gate_jobs([job], {job.id: reqs}, gate)
        assert passing == [job], results[job.id]
        assert results[job.id].education_penalty == 0.0

    def test_rejection_reason_contains_enrolment_evidence(self):
        job, reqs = self._extract()
        gate = HardConstraintGate(user_max_education_level=parse_education_level("bac+1"))
        result = gate.check(reqs)
        edu_reason = next(r for r in result.reasons if r.type == "education")
        assert edu_reason.requirement == "enrolled in bac+3"
        assert edu_reason.candidate_value == "bac+1"
        assert edu_reason.span is not None
        assert "bac" in edu_reason.span.text.lower()

    def test_candidate_who_cannot_be_enrolled_in_bac3_is_rejected_before_scoring(self):
        """End-to-end via gate_jobs(): a held Bac+1 candidate cannot be in
        Bac+3 yet, so the job never reaches scoring."""
        job, reqs = self._extract()
        gate = HardConstraintGate(user_max_education_level=parse_education_level("bac+1"))
        passing, results = gate_jobs([job], {job.id: reqs}, gate)
        assert passing == []
        assert results[job.id].passes is False

    def test_no_longer_rejected_with_no_limit(self):
        """Changing the profile setting to 'No limit' (max_education=None)
        must make the same job pass again -- the ceiling is fully
        configuration-driven, not hard-coded."""
        job, reqs = self._extract()
        gate = HardConstraintGate(user_max_education_level=None)
        result = gate.check(reqs)
        assert result.passes

    def test_accepted_with_max_bac4_or_higher(self):
        """Raising the configured max to Bac+4 must accept the same job."""
        job, reqs = self._extract()
        for level_str in ("bac+4", "bac+5"):
            gate = HardConstraintGate(user_max_education_level=parse_education_level(level_str))
            assert gate.check(reqs).passes, f"Should pass with max_education={level_str}"
