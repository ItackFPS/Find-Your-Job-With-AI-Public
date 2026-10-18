"""Issue #40: held degrees vs current enrolment vs preparing a diploma.

Semantics confirmed by the owner (2026-09-17):
  * profile.max_education is the highest level already HELD by the candidate;
  * a posting may require a held degree, current ENROLMENT at a level, or that
    the candidate is PREPARING a diploma — these are different requirements;
  * an enrolment range ("Étudiant(e) en Bac+3 à Bac+4") starts at its lowest
    level;
  * "Vous préparez un Bac+5" never hard-rejects by itself (Job 39), unless the
    level is made explicitly mandatory ("minimum", "requis", "obligatoire");
  * a qualifier attached to another requirement ("…, avec une spécialité IA
    ou équivalent") must not change the education reading.

Being enrolled in year E requires having completed year E-1, so an ENROLLED
requirement rejects only a candidate whose held level makes that impossible.
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.requirements.education import extract_education, parse_education_level
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import (
    EducationKind,
    EducationRequirement,
    EvidenceSource,
    Importance,
    JobRequirements,
)


def _passes(description: str, held: str) -> bool:
    job = Job(id="j1", source="test", title="Stage Développeur", description=description)
    requirements = RequirementExtractor(user_skills=["python"]).extract(job)
    gate = HardConstraintGate(user_max_education_level=parse_education_level(held))
    passing, _results = gate_jobs([job], {job.id: requirements}, gate)
    return passing == [job]


# ---------------------------------------------------------------------------
# Held degrees — unchanged (ADR 0004)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["Bac+3 requis.", "Titulaire d'un Bac+3.", "Licence exigé.", "Bac+3 minimum."],
)
def test_held_bac3_requirement_rejects_held_bac2_and_accepts_held_bac3(text):
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.HELD
    assert not _passes(text, "bac+2")
    assert _passes(text, "bac+3")


# ---------------------------------------------------------------------------
# Current enrolment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Profil : étudiant(e) en Bac+3 en informatique.",
        "Vous êtes actuellement en Bac+3 développement web.",
    ],
)
def test_bac2_holder_can_be_currently_enrolled_in_bac3(text):
    edu = extract_education(text)
    assert edu is not None
    assert (edu.kind, edu.enrolment_level_min) == (EducationKind.ENROLLED, 3)
    assert _passes(text, "bac+2")
    assert not _passes(text, "bac+1")


FINAL_YEAR = "Profil : Étudiant(e) en dernière année d'école d'ingénieur en informatique."


def test_final_year_engineering_student_is_enrolment_not_a_held_bac5():
    edu = extract_education(FINAL_YEAR)
    assert edu is not None
    assert (edu.kind, edu.enrolment_level_min) == (EducationKind.ENROLLED, 5)


def test_final_year_student_candidate_satisfies_enrolment_but_not_held_bac5():
    # A final-year engineering student has completed Bac+4.
    assert _passes(FINAL_YEAR, "bac+4")
    held_bac5 = "Titulaire d'un diplôme d'ingénieur (Bac+5) requis."
    assert not _passes(held_bac5, "bac+4")


def test_final_year_enrolment_is_impossible_for_a_bac2_holder():
    assert not _passes(FINAL_YEAR, "bac+2")


BEZANNES = (
    "Profil recherché\n\n"
    "Étudiant(e) en Bac +3 à Bac + 4, développement web, Data science, informatique, "
    "avec un spécialité IA ou équivalent.\n\n"
    "Connaissances de base en HTML, CSS, PHP et JavaScript."
)


@pytest.mark.parametrize(
    "text",
    [BEZANNES, BEZANNES.replace(" ou équivalent", ""), "Étudiant(e) en Bac+3 à Bac+4."],
)
def test_enrolment_range_uses_its_lowest_level(text):
    edu = extract_education(text)
    assert edu is not None
    assert (edu.kind, edu.enrolment_level_min) == (EducationKind.ENROLLED, 3)
    assert _passes(text, "bac+2")
    assert _passes(text, "bac+3")
    assert not _passes(text, "bac+1")


def test_unrelated_ou_equivalent_does_not_change_the_education_reading():
    with_leak = extract_education(BEZANNES)
    without = extract_education(BEZANNES.replace(" ou équivalent", ""))
    assert with_leak is not None and without is not None
    assert (with_leak.kind, with_leak.importance, with_leak.is_or_alternative) == (
        without.kind, without.importance, without.is_or_alternative,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Bac+5 en informatique, avec une spécialisation en IA ou équivalent.",
        "Bac+5 en informatique et une expérience en Java ou équivalent.",
    ],
)
def test_ou_equivalent_attached_to_another_requirement_does_not_soften_or_harden_a_held_degree(text):
    edu = extract_education(text)
    assert edu is not None
    assert edu.importance == Importance.UNKNOWN
    assert not edu.is_or_alternative
    assert _passes(text, "bac+2")  # a bare, unqualified mention never hard-rejects


def test_ou_equivalent_attached_to_the_degree_still_applies():
    edu = extract_education("Bac+5 en électronique, systèmes embarqués, ou équivalent.")
    assert edu is not None
    assert (edu.importance, edu.is_or_alternative) == (Importance.PREFERRED, True)


@pytest.mark.parametrize(
    "text, level_min",
    [
        # "Dernière année de …" without a leading "en".
        ("Votre profil\n\nDernière année de Master en informatique avec, idéalement, spécialité en testing.", 5),
        # "Master 2" must not also count as a generic "Master" (entry year 4).
        ("Actuellement en Master 2 / Bac+5 en École d'ingénieur ou Université, vous vous spécialisez en data.", 5),
        ("Étudiant(e) en Master orienté Développeur Web Full Stack.", 4),
        # Final year of a Bac+5 programme is year 5.
        ("Vous êtes en dernière année d'un cycle BAC+5.", 5),
        # "ou de Master 2" stays inside the final-year scope.
        ("Étudiant(e) en dernière année d'école d'ingénieurs ou de Master 2 informatique.", 5),
    ],
)
def test_enrolment_stage_from_real_postings(text, level_min):
    edu = extract_education(text)
    assert edu is not None
    assert (edu.kind, edu.enrolment_level_min) == (EducationKind.ENROLLED, level_min)


@pytest.mark.parametrize(
    "text",
    [
        # An engineering-school student's year is not stated: a Bac+2 holder
        # can be in year 3 through parallel admission.
        "Étudiant(e) en école d'ingénieur ou en Master spécialisé en Génie Civil.",
        # Real posting: typographic apostrophe, so "école d’ingénieur" is not
        # even an education mention yet (#42) — the programme word still counts.
        "Étudiant(e) en école d\u2019ingénieur ou en Master spécialisé en Génie Civil.",
        "Étudiant(e) en licence ou en Master informatique.",
        # Real postings: comma-separated alternative; "ou en …" after a final-year cue.
        "Vous êtes étudiant(e) en Master, en École d'ingénieur ou en formation équivalente.",
        "Étudiant(e) en dernière année de Master ou en école d'ingénieur, avec une spécialisation en informatique.",
        # "cursus/cycle/formation Bac+5" names the programme level, not year 5.
        "Étudiant en cursus BAC+5 en école d'ingénieurs ou formation universitaire.",
        "Étudiant(e) en formation Bac+5 en informatique.",
    ],
)
def test_enrolment_with_an_unknown_stage_alternative_never_rejects(text):
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.ENROLLED
    assert edu.enrolment_level_min is None
    assert _passes(text, "bac+2")


def test_actuellement_en_recherche_is_not_an_enrolment_cue():
    text = "Profil actuellement en recherche d'un stage Bac+5."
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.HELD


# ---------------------------------------------------------------------------
# Preparing a diploma (Job 39)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Vous préparez un Bac+5 en informatique.",
        "Vous préparez un Bac+5, idéalement en informatique.",
        "Tu prépares un diplôme d'ingénieur ou équivalent.",
        "Candidat en cours de préparation d'un Master 2.",
    ],
)
def test_preparing_a_diploma_never_hard_rejects_by_itself(text):
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.PREPARING
    assert _passes(text, "bac+2")


@pytest.mark.parametrize(
    "text",
    [
        "Vous préparez un diplôme Bac+5 en informatique.",
        "Vous préparez un diplôme de niveau Bac+4/5, idéalement en data.",
        "Tu prépares un diplôme Bac+3 à Bac+5 en automatisme.",
        "Étudiant.e préparant un diplôme de niveau BAC+5 en informatique.",
    ],
)
def test_preparing_a_diplome_is_not_holding_it(text):
    # The noun "diplôme" is not a held cue; only the participle "diplômé" is
    # (issue #63).
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.PREPARING
    assert _passes(text, "bac+2")


@pytest.mark.parametrize(
    "text, level",
    [
        ("Diplômé(e) d'un Bac+5 en informatique.", 5),
        ("Vous êtes diplômée d'un Bac+3.", 3),
        ("Étudiant(e) en Bac+5, diplômé d'un Bac+3 requis.", 3),
    ],
)
def test_diplome_participle_is_still_a_held_cue(text, level):
    found = [
        r for r in [extract_education(text)] if r is not None and r.kind == EducationKind.HELD
    ]
    assert found and found[0].level_bac_plus == level


@pytest.mark.parametrize(
    "text",
    [
        "Vous préparez un diplôme de niveau Bac+5 obligatoire.",
        "Vous préparez un Bac+5 minimum en école d'ingénieur.",
        "Vous préparez un Bac+5, requis pour ce poste.",
        "Bac+5 obligatoire.",
        "Bac+5 requis.",
        "Bac+5 minimum.",
    ],
)
def test_explicit_mandatory_bac5_rejects_a_bac2_holder(text):
    assert not _passes(text, "bac+2")


# ---------------------------------------------------------------------------
# No soft penalty for enrolment / preparing; LLM merge keeps the kind
# ---------------------------------------------------------------------------

def test_enrolment_and_preparing_get_no_soft_education_penalty():
    for text in (BEZANNES, "Vous préparez un Bac+5 en informatique."):
        job = Job(id="j1", source="test", title="Stage", description=text)
        requirements = RequirementExtractor(user_skills=["python"]).extract(job)
        gate = HardConstraintGate(user_max_education_level=2)
        _passing, results = gate_jobs([job], {job.id: requirements}, gate, education_preferred_penalty=0.2)
        assert results[job.id].education_penalty == 0.0, text


def test_llm_merge_keeps_kind_enrolment_level_and_experience_alternative():
    heuristic_edu = EducationRequirement(
        level_bac_plus=4, level_label="Bac+4", importance=Importance.UNKNOWN,
        kind=EducationKind.ENROLLED, enrolment_level_min=3, is_experience_alternative=True,
    )
    llm_edu = EducationRequirement(
        level_bac_plus=4, level_label="Bac+4", importance=Importance.MANDATORY,
        source=EvidenceSource.LLM_CORROBORATED,
    )
    merged = RequirementExtractor._merge(
        JobRequirements(education=heuristic_edu), JobRequirements(education=llm_edu)
    )
    assert merged.education is not None
    assert merged.education.importance == Importance.MANDATORY
    assert (merged.education.kind, merged.education.enrolment_level_min) == (EducationKind.ENROLLED, 3)
    assert merged.education.is_experience_alternative is True
