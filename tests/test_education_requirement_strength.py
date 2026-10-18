"""Education requirement strength: which wordings may hard-reject (ADR 0004).

A level above ``profile.max_education`` hard-rejects only when the posting
makes it a requirement:

- an explicit mandatory cue ("exigé", "requis", "minimum", "required"), or
- a level stated as the bar with only an equivalence softener
  ("Bac+4 ou équivalent").

A desirability cue ("souhaité", "apprécié", "un plus", "preferred") is a
preference: soft score penalty, never a hard reject. An explicit
non-eliminatory cue ("non éliminatoire", "pas obligatoire", "not required")
in the mention's sentence makes the mention OPTIONAL: no reject, no penalty.
Negated mandatory words ("not required", "non obligatoire") are never read
as mandatory cues.

Regression origin: the synthetic benchmark job bm46 ("Bac+2 en informatique.
Un niveau Bac+5 est apprécié mais absolument pas un critère éliminatoire")
was hard-rejected for a Bac+2 profile.
"""
from __future__ import annotations

import pytest

from src.models.job import Job
from src.requirements.education import extract_education, parse_education_level
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import (
    EducationRequirement,
    EvidenceSource,
    Importance,
    JobRequirements,
)

BM46 = (
    "Profil recherché : Bac+2 en informatique. Un niveau Bac+5 est apprécié mais "
    "absolument pas un critère éliminatoire — nous recrutons avant tout sur le "
    "potentiel."
)


def _gate(description: str, max_education: str = "bac+2"):
    job = Job(id="j1", source="test", title="Dev", description=description)
    reqs = RequirementExtractor(user_skills=["python"]).extract(job)
    gate = HardConstraintGate(
        user_max_education_level=parse_education_level(max_education)
    )
    passing, results = gate_jobs([job], {job.id: reqs}, gate)
    return bool(passing), results[job.id], reqs


class TestNonEliminatoryCues:
    @pytest.mark.parametrize(
        "text",
        [
            "Bac+5 apprécié, non éliminatoire.",
            "Un Master est apprécié mais n'est pas un critère éliminatoire.",
            "Diplôme Bac+5 non obligatoire.",
            "Bac+5 pas forcément nécessaire pour ce poste.",
            "Master's degree not required.",
            "A Master's degree is optional.",
            "Bac+5 facultatif.",
        ],
    )
    def test_explicitly_non_eliminatory_mention_is_optional(self, text):
        edu = extract_education(text)
        assert edu is not None
        assert edu.importance == Importance.OPTIONAL

    @pytest.mark.parametrize(
        "text",
        [
            BM46,
            "Bac+5 apprécié, non éliminatoire.",
            "Diplôme Bac+5 non obligatoire.",
            "Master's degree not required.",
        ],
    )
    def test_non_eliminatory_mention_neither_rejects_nor_penalises(self, text):
        passes, result, _ = _gate(text)
        assert passes, result.reasons
        assert result.education_penalty == 0.0

    def test_negation_about_another_requirement_keeps_the_mandatory_cue(self):
        # "non obligatoire" qualifies the driving licence, not the degree.
        edu = extract_education("Bac+5 requis, permis B non obligatoire.")
        assert edu is not None
        assert edu.importance == Importance.MANDATORY
        passes, _, _ = _gate("Bac+5 requis, permis B non obligatoire.")
        assert not passes

    def test_negation_in_another_sentence_does_not_apply(self):
        text = "Bac+5 exigé. Le permis B n'est pas obligatoire."
        assert extract_education(text).importance == Importance.MANDATORY
        assert not _gate(text)[0]


class TestPreferredIsNotACeiling:
    @pytest.mark.parametrize(
        "text",
        [
            "Bac+5 souhaité.",
            "Niveau Bac+5 apprécié pour ce poste.",
            "Un Master serait un plus.",
            "Bac+5 souhaité ou équivalent.",
        ],
    )
    def test_desirable_level_passes_with_soft_penalty(self, text):
        passes, result, reqs = _gate(text)
        assert reqs.education is not None
        assert reqs.education.importance == Importance.PREFERRED
        assert passes, result.reasons
        assert result.education_penalty > 0 or reqs.education.is_or_alternative

    def test_level_stated_as_the_bar_with_equivalence_still_rejects(self):
        # Owner's documented case (ADR 0004): "ou équivalent" accepts an
        # equivalent diploma, it does not lower the bar.
        passes, result, _ = _gate("Bac+4 ou équivalent.", max_education="bac+3")
        assert not passes
        assert result.reasons[0].type == "education"

    def test_mandatory_level_still_rejects(self):
        assert not _gate("Bac+5 exigé.")[0]
        assert not _gate("Bac+3 minimum requis.")[0]

    def test_mandatory_lower_level_with_desirable_higher_level_passes(self):
        passes, _, reqs = _gate("Bac+2 en informatique exigé. Bac+5 apprécié.")
        assert passes
        assert reqs.education.level_bac_plus == 2

    def test_stated_bar_wins_over_a_higher_desirable_level(self):
        # The enforceable requirement is the one checked, whatever the order.
        edu = extract_education("Bac+3 ou équivalent. Bac+5 apprécié.")
        assert edu.level_bac_plus == 3
        assert not _gate("Bac+3 ou équivalent. Bac+5 apprécié.")[0]
        assert _gate("Bac+2 ou équivalent. Bac+5 apprécié.")[0]

    def test_llm_corroborated_preferred_is_not_a_ceiling(self):
        reqs = JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Bac+5",
                importance=Importance.PREFERRED,
                raw_text="Bac+5",
                source=EvidenceSource.LLM_CORROBORATED,
            )
        )
        gate = HardConstraintGate(user_max_education_level=2)
        assert gate.check(reqs).passes


def test_bm46_benchmark_text_is_not_rejected():
    passes, result, reqs = _gate(BM46)
    assert passes, [r.message for r in result.reasons]
    assert reqs.education.importance == Importance.OPTIONAL
