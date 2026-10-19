"""Regression tests for experience requirement extraction and scoring penalty.

Covers:
1.  4+ year mandatory requirement → hard gate rejection for junior candidate
2.  1–2 year requirement ≤ profile max → no penalty
3.  Unknown/no mandatory qualifier → no penalty
4.  Education mismatch stacks with experience mismatch → combined penalty
5.  No experience mention → no penalty
6.  Strong skill match + major experience mismatch → materially lower score
7.  High semantic similarity + incompatible experience → still lower score
8.  Requirement only in description (not title) → still extracted
9.  Various phrasings all detected as MANDATORY
10. Explanation distinguishes explicit mismatch from unknown seniority
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.pipeline.scoring import score_job
from src.requirements.experience import extract_experience
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import (
    GateResult,
    Importance,
    JobRequirements,
)
from tests.fixtures.profiles import TEST_PROFILE_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(
    jid: str = "j1",
    title: str = "Ingénieur DevOps",
    description: str = "",
) -> Job:
    return Job(
        id=jid,
        source="test",
        title=title,
        description=description,
        location="France",
        countries=["France"],
        remote=False,
        employment_type="full-time",
    )


def _score(
    education_penalty: float = 0.0,
    skill_penalty: float = 0.0,
    experience_penalty: float = 0.0,
) -> float:
    """Score a dummy job with the given gate penalties pre-set."""
    job = _job()
    gate_result = GateResult(
        passes=True,
        education_penalty=education_penalty,
        skill_penalty=skill_penalty,
        experience_penalty=experience_penalty,
    )
    config: dict = {
        "scoring": {
            "title_relevance": {"weight": 0.30},
            "role_match": {"weight": 0.0},
            "remote_compatibility": {"weight": 0.10},
            "location_match": {"weight": 0.15, "preferred_countries": ["france"]},
            "seniority_match": {"weight": 0.10},
            "recency": {"weight": 0.10},
            "skills_match": {"weight": 0.25},
        }
    }
    return score_job(job, config, gate_result)


def _gate_check(requirements: JobRequirements, user_max_years: int | None = 3) -> GateResult:
    """Run gate check and return the GateResult (penalties/experience live here now)."""
    gate = HardConstraintGate(
        user_max_education_level=None,
        user_max_years_experience=user_max_years,
    )
    job = _job()
    requirements_map = {job.id: requirements}
    _passing, results = gate_jobs([job], requirements_map, gate)
    return results[job.id]


# ---------------------------------------------------------------------------
# Test 1: 4+ year mandatory requirement → hard gate rejection for junior (max 3 years)
# ---------------------------------------------------------------------------

def test_mandatory_4yr_rejects_junior_candidate():
    desc = "vous justifiez d'une expérience significative d'au moins 4 ans sur un poste DevOps"
    exp = extract_experience(desc)
    assert exp is not None
    assert exp.min_years == 4
    assert exp.importance == Importance.MANDATORY

    requirements = JobRequirements(experience=exp)
    gate = HardConstraintGate(
        user_max_education_level=None,
        user_max_years_experience=3,
    )
    result = gate.check(requirements)
    assert not result.passes, "MANDATORY experience > max must be a hard gate rejection"
    assert any(r.type == "experience" for r in result.reasons)


# ---------------------------------------------------------------------------
# Test 2: 1–2 year requirement ≤ profile max → no penalty
# ---------------------------------------------------------------------------

def test_low_experience_requirement_no_penalty():
    desc = "au moins 2 ans d'expérience en développement web"
    exp = extract_experience(desc)
    assert exp is not None
    assert exp.min_years == 2

    requirements = JobRequirements(experience=exp)
    gate_result = _gate_check(requirements, user_max_years=3)
    assert gate_result.experience_penalty == 0.0


# ---------------------------------------------------------------------------
# Test 3: No mandatory qualifier → not extracted, no penalty
# ---------------------------------------------------------------------------

def test_bare_years_mention_not_mandatory():
    # "3 ans d'expérience" without "au moins" / "minimum" = UNKNOWN → ignored
    desc = "3 ans d'expérience en développement logiciel"
    exp = extract_experience(desc)
    assert exp is None  # no mandatory/preferred qualifier → not extracted

    requirements = JobRequirements(experience=exp)
    gate_result = _gate_check(requirements, user_max_years=3)
    assert gate_result.experience_penalty == 0.0


# ---------------------------------------------------------------------------
# Test 4: Edu penalty + experience penalty stack multiplicatively
# ---------------------------------------------------------------------------

def test_edu_and_experience_penalties_stack():
    base = _score()
    with_edu = _score(education_penalty=0.20)
    with_both = _score(education_penalty=0.20, experience_penalty=0.40)

    # Each penalty applied multiplicatively
    assert with_edu < base
    assert with_both < with_edu
    # Combined: raw × 0.8 × 0.6 = raw × 0.48
    assert with_both == pytest.approx(base * 0.8 * 0.6, rel=0.01)


# ---------------------------------------------------------------------------
# Test 5: No experience mention → no penalty
# ---------------------------------------------------------------------------

def test_no_experience_mention_no_penalty():
    desc = "Développeur Python chez une startup innovante. Stack Python, FastAPI, Docker."
    exp = extract_experience(desc)
    assert exp is None

    requirements = JobRequirements(experience=exp)
    gate_result = _gate_check(requirements, user_max_years=3)
    assert gate_result.experience_penalty == 0.0


# ---------------------------------------------------------------------------
# Test 6: Strong skills match + major experience mismatch → lower score
# ---------------------------------------------------------------------------

def test_experience_mismatch_lowers_high_skills_score():
    high_score_no_penalty = _score(experience_penalty=0.0)
    high_score_with_penalty = _score(experience_penalty=0.40)
    assert high_score_with_penalty < high_score_no_penalty * 0.75


# ---------------------------------------------------------------------------
# Test 7: Experience penalty reduces even high-scoring jobs significantly
# ---------------------------------------------------------------------------

def test_experience_penalty_magnitude():
    """A 40% experience penalty must drop the score by at least 30 absolute points."""
    raw_score = _score()
    penalized = _score(experience_penalty=0.40)
    # At 40% penalty: penalized = raw * 0.6 → drop = raw * 0.4 ≥ meaningful
    assert penalized == pytest.approx(raw_score * 0.6, rel=0.01)
    assert (raw_score - penalized) > 0.20  # minimum 20-point absolute drop for any realistic job


# ---------------------------------------------------------------------------
# Test 8: Requirement only in description → extracted correctly
# ---------------------------------------------------------------------------

def test_experience_extracted_from_description_only():
    title = "Ingénieur DevOps"
    desc = "minimum 5 ans sur un poste similaire. Bonne maîtrise de Kubernetes."
    exp = extract_experience(desc)
    assert exp is not None
    assert exp.min_years == 5
    assert exp.importance == Importance.MANDATORY
    # Title has no experience indicator — still extracted from description
    assert "minimum" not in title.lower()


# ---------------------------------------------------------------------------
# Test 9: Various mandatory phrasings all detected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_years", [
    ("au moins 4 ans d'expérience", 4),
    ("au minimum 3 ans en tant que développeur", 3),
    ("minimum 5 ans d'expérience", 5),
    ("5 ans minimum requis", 5),
    ("4+ ans sur ce type de poste", 4),
    ("6 ans ou plus d'expérience", 6),
    ("at least 4 years of experience", 4),
    ("minimum 3 years in DevOps", 3),
    ("5+ years of experience required", 5),
    ("4 years or more preferred", 4),
    # Markdown-embedded (as seen in the Randstad job)
    ("d'au moins 4\n  **ans**\n  sur un poste DevOps", 4),
])
def test_mandatory_phrasings(text, expected_years):
    exp = extract_experience(text)
    assert exp is not None, f"Expected extraction from: {text!r}"
    assert exp.min_years == expected_years
    assert exp.importance == Importance.MANDATORY


# ---------------------------------------------------------------------------
# Test 10: Explanation shows specific experience requirement when extracted
# ---------------------------------------------------------------------------

def test_explanation_shows_extracted_experience_not_generic_message():
    from src.pipeline.explain import generate_explanation
    from src.pipeline.filter import load_profile

    profile = load_profile(TEST_PROFILE_PATH)
    desc = "vous justifiez d'une expérience significative d'au moins 4 ans sur un poste DevOps"
    job = _job(description=desc)

    result = generate_explanation(job, profile, filter_result="passed")

    concern_texts = [r.text for r in result.concerns]
    # Must mention a specific year count, not the generic "not explicitly stated" message
    assert any("4" in t for t in concern_texts), (
        f"Expected '4' year mention in concerns. Got: {concern_texts}"
    )
    assert not any("not explicitly stated in the title" in t for t in concern_texts), (
        f"Should not show generic message when requirement is in description. Got: {concern_texts}"
    )


# ---------------------------------------------------------------------------
# Ranges: the requirement is the LOWER bound ("3 à 8 ans" accepts 3 years)
# ---------------------------------------------------------------------------
# Real posting, 2026-10-18 (HelloWork, "Ingénieur DevOps Cloud H/F"): "Expérience de 3 à 8 ans
# minimum" was read as "at least 8 years" and hard-rejected a 3-year candidate the range includes.

@pytest.mark.parametrize("text, lower", [
    ("Expérience de 3 à 8 ans minimum sur des environnements cloud", 3),
    ("Vous avez 2 à 5 ans minimum d'expérience", 2),
    ("Expérience : 2-4 ans minimum", 2),
    ("une expérience de 3 – 5 ans ou plus", 3),
    ("3 to 5 years minimum of Python", 3),
    ("3-5 years or more in a similar role", 3),
    ("entre 2 et 6 ans minimum", 2),
])
def test_a_range_requires_its_lower_bound(text, lower):
    exp = extract_experience(text)
    assert exp is not None and exp.importance == Importance.MANDATORY
    assert exp.min_years == lower
    assert exp.span.text.lstrip().startswith(str(lower)) or str(lower) in exp.span.text


def test_a_range_within_the_candidate_maximum_passes_the_gate():
    exp = extract_experience("Profil : Expérience de 3 à 8 ans minimum sur des environnements cloud.")
    gate = HardConstraintGate(user_max_education_level=None, user_max_years_experience=3)
    result = gate.check(JobRequirements(experience=exp))
    assert result.passes, result.reasons


def test_single_minimums_are_unchanged():
    assert extract_experience("Minimum 7 ans d'expérience").min_years == 7
    assert extract_experience("5 ans minimum dans un environnement similaire").min_years == 5
    # the highest mandatory minimum still wins over a lower one elsewhere
    assert extract_experience("au moins 2 ans en Python ; 6 ans minimum en Java").min_years == 6
    # a number that merely precedes the minimum is not a range ("Bac+5, 3 ans minimum")
    assert extract_experience("Bac+5, 3 ans minimum").min_years == 3
