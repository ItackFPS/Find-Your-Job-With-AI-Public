"""Education by French/EQF level 3–8 (profile v2, F5.3): ADR 0004 cases on levels + new names."""

from __future__ import annotations

import pytest

from src.models.levels import QUALIFICATION_LEVELS, bac_plus_to_level, level_to_max_bac_plus
from src.models.profile import JobSearchProfile
from src.requirements.education import candidate_education, extract_education
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import JobRequirements


def _v2(level: int | None, name: str = "BUT", in_progress: bool = False, **extra) -> JobSearchProfile:
    edu = [] if level is None else [{"id": "e1", "qualification": name, "level": level, "in_progress": in_progress}]
    return JobSearchProfile(profile_version=2, education=edu, **extra)


def _gate(profile: JobSearchProfile) -> HardConstraintGate:
    max_bac, label = candidate_education(profile)
    return HardConstraintGate(user_max_education_level=max_bac, user_education_label=label)


def _check(profile: JobSearchProfile, text: str):
    return _gate(profile).check(JobRequirements(education=extract_education(text)))


@pytest.mark.parametrize("level", range(3, 9))
def test_level_threshold_is_the_exact_inverse(level):
    for bac_plus in range(-1, 9):
        assert (bac_plus_to_level(bac_plus) <= level) == (bac_plus <= level_to_max_bac_plus(level))


def test_profile_names_map_to_the_nomenclature():
    assert [QUALIFICATION_LEVELS[n] for n in ("CAP", "Bac", "BTS", "DUT", "BUT", "Licence", "Licence pro",
                                              "Master", "Diplôme d'ingénieur", "Doctorat")] == [
        3, 4, 5, 5, 6, 6, 6, 7, 7, 8]


def test_candidate_education_prefers_held_v2_levels():
    assert candidate_education(_v2(6)) == (4, "level 6 (BUT)")
    # in progress does not count as held; falls back to v1 max_education
    assert candidate_education(_v2(7, "Master", in_progress=True, max_education="bac+3")) == (3, None)
    assert candidate_education(JobSearchProfile(max_education="bac+2")) == (2, None)   # v1
    assert candidate_education(_v2(None)) == (None, None)


# ADR 0004 cases, on levels
def test_mandatory_higher_level_rejects_with_a_level_message():
    r = _check(_v2(6), "Bac+5 exigé en informatique.")
    assert not r.passes
    assert r.reasons[0].message == "Job requires level 7 (Bac+5) (mandatory); you declared level 6 (BUT)."
    assert r.reasons[0].candidate_value == "level 6 (BUT)"


def test_same_level_passes_even_when_bac_plus_differs():
    # Bac+4 is level 6 like a Licence/BUT: the levels match, so it is not a rejection.
    assert _check(_v2(6), "Bac+4 minimum requis.").passes
    assert _check(_v2(6, "Licence"), "Titulaire d'une licence exigé.").passes


def test_desirable_level_is_a_soft_penalty_only():
    profile = _v2(5, "BTS")
    req = JobRequirements(education=extract_education("Bac+5 souhaité."))
    passing, results = gate_jobs([type("J", (), {"id": "j", "title": "Dev", "description": ""})()],
                                 {"j": req}, _gate(profile))
    assert passing and results["j"].education_penalty > 0


def test_experience_alternative_never_rejects():
    assert _check(_v2(5, "BTS"), "Bac+5 ou expérience équivalente exigé.").passes


def test_unknown_importance_never_rejects():
    assert _check(_v2(4, "Bac"), "Formation Bac+5 en informatique.").passes


def test_enrolment_is_worded_in_levels():
    r = _check(_v2(5, "BTS"), "Vous êtes actuellement en Master 2.")
    assert not r.passes and "level 7" in r.reasons[0].message and "level 5 (BTS)" in r.reasons[0].message


def test_foreign_degree_uses_the_level_the_user_picked():
    foreign = JobSearchProfile(profile_version=2, education=[
        {"id": "f", "qualification": "MSc (UK)", "level": 7, "foreign": True}])
    r = _check(foreign, "Bac+5 exigé.")
    assert r.passes


# New qualification names in offers
@pytest.mark.parametrize("text,bac_plus,label", [
    ("Titulaire d'un BUT informatique exigé", 3, "BUT"),
    ("Diplôme B.U.T. GEII requis", 3, "BUT"),
    ("DEUG requis", 2, "DEUG"),
    ("Titulaire d'un CAP exigé", -1, "CAP"),
    ("Diplôme BEP requis", -1, "BEP"),
])
def test_new_qualification_names_are_extracted(text, bac_plus, label):
    edu = extract_education(text)
    assert edu is not None and (edu.level_bac_plus, edu.level_label) == (bac_plus, label)


@pytest.mark.parametrize("text", ["Connaissance du théorème CAP.", "but we also need Rust", "Avoir un bon cap."])
def test_ambiguous_words_are_not_degrees(text):
    edu = extract_education(text)
    assert edu is None or edu.level_label not in ("CAP", "BUT")


def test_but_holder_meets_a_licence_requirement_and_not_a_master():
    assert _check(_v2(6), "Licence exigée.").passes
    assert not _check(_v2(6), "Master exigé.").passes
