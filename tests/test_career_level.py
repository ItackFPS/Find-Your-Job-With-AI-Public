"""Career-stage check (src/requirements/career_level.py, F5.2) and its gate/filter wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models.profile import JobSearchProfile
from src.pipeline.filter import build_filter_config, load_filters
from src.requirements.career_level import (
    allowed_max_rank,
    assess_career_level,
    career_config,
    job_level_signals,
    title_vocabulary,
    years_to_rank,
)
from src.requirements.experience import extract_experience
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import ConstraintState, ExperienceRequirement, Importance, JobRequirements

ENGINE = load_filters("config/filters.yaml")
VOCAB = title_vocabulary(ENGINE["seniority_exclusions"])
JUNIOR = allowed_max_rank(["graduate", "junior"], stretch=False)


def _job(title: str, description: str = "", employment_type: str | None = None, jid: str = "j1"):
    return SimpleNamespace(id=jid, title=title, description=description, employment_type=employment_type)


def _assess(title, description="", employment_type=None, years=None, importance=Importance.MANDATORY,
            max_rank=JUNIOR):
    exp = ExperienceRequirement(min_years=years, importance=importance) if years is not None else None
    return assess_career_level(job_level_signals(title, description, employment_type, exp, VOCAB), max_rank)


def _profile(levels, stretch=False, **extra):
    return JobSearchProfile(profile_version=2, targets={"levels": levels, "stretch": stretch}, **extra)


@pytest.mark.parametrize("years,level", [(0, 1), (1, 2), (2, 2), (3, 3), (4, 3), (5, 4), (7, 4), (8, 5), (15, 5)])
def test_years_map_to_career_ranks(years, level):
    assert years_to_rank(years) == level


def test_stretch_allows_one_level_up():
    assert allowed_max_rank(["junior"], False) == 2 and allowed_max_rank(["junior"], True) == 3
    assert allowed_max_rank([], True) is None


def test_fitting_offer_is_satisfied():
    a = _assess("Développeur Python Junior", years=1)
    assert a.state == ConstraintState.SATISFIED and not a.conflicts


def test_people_management_with_a_senior_title_rejects():
    a = _assess("Senior Engineering Manager", "Vous encadrerez une équipe de 6 développeurs.")
    assert a.state == ConstraintState.VIOLATED
    assert {s.kind for s in a.conflicts} == {"title", "responsibilities"}


def test_people_management_with_fitting_years_goes_to_review():
    a = _assess("Lead developer", "You will manage a team of 3.", years=2)
    assert a.state == ConstraintState.UNKNOWN


def test_lower_level_offer_is_not_a_conflict():
    a = _assess("Stagiaire data", employment_type="Stage", max_rank=allowed_max_rank(["senior"], False))
    assert a.state == ConstraintState.SATISFIED


def test_no_target_is_not_evaluated():
    assert _assess("Senior Engineer", max_rank=None).state == ConstraintState.UNKNOWN


# Benchmark (plan F5.2): a title word, alone or contradicted by the stated years, never rejects.
TITLE_ONLY = [
    ("Senior Data Engineer", "", None),
    ("Développeur Sénior Python", "Rejoignez une équipe produit.", None),
    ("Ingénieur DevOps confirmé", "", None),
    ("Lead Developer React", "Vous travaillerez avec 5 développeurs.", None),
    ("Tech Lead Backend", "", None),
    ("Head of Data", "", None),
    ("Responsable informatique", "", None),
    ("Chef de projet digital", "", None),
    ("Principal Software Engineer", "", None),
    ("Staff Engineer Platform", "", None),
    ("Directeur technique", "", None),
    ("CTO startup", "", None),
    ("Senior Engineer", "2 ans d'expérience minimum", 2),
    ("Lead dev", "Débutant accepté, 1 an d'expérience", 1),
    ("Data Scientist Senior", "Experience: 0-2 years", 0),
]


@pytest.mark.parametrize("title,description,years", TITLE_ONLY)
def test_title_only_conflicts_go_to_review_never_reject(title, description, years):
    a = _assess(title, description, years=years)
    assert a.state == ConstraintState.UNKNOWN, (title, a.message)
    assert any(s.kind == "title" for s in a.conflicts)


def test_gate_flags_title_only_conflict_and_keeps_the_job():
    cfg = career_config(_profile(["graduate", "junior"]), ENGINE["seniority_exclusions"])
    gate = HardConstraintGate(user_max_education_level=None, career_level=cfg)
    jobs = [_job("Senior Python Developer", jid="a"),
            _job("Engineering Manager Senior", "Vous managerez une équipe de 8 personnes.", jid="b"),
            _job("Développeur junior", jid="c")]
    passing, results = gate_jobs(jobs, {j.id: JobRequirements() for j in jobs}, gate)
    assert [j.id for j in passing] == ["a", "c"]
    assert results["a"].pass_with_uncertainty and results["a"].states["seniority"] == ConstraintState.UNKNOWN
    assert any(f.startswith("Career level:") for f in results["a"].flags)
    assert results["b"].reasons[0].type == "career_level" and not results["b"].passes
    assert results["c"].states["seniority"] == ConstraintState.SATISFIED and not results["c"].uncertainty


def test_gate_uses_the_extracted_years():
    cfg = career_config(_profile(["junior"]), ENGINE["seniority_exclusions"])
    gate = HardConstraintGate(user_max_education_level=None, career_level=cfg)
    job = _job("Senior Engineer", "Vous managerez une équipe. 2 ans d'expérience minimum requis.")
    req = JobRequirements(experience=extract_experience(job.description))
    result = gate.check(req, job)
    assert result.passes and result.uncertainty            # the years fit: review, not reject


def test_v1_profile_keeps_the_keyword_rejection_and_no_career_check():
    v1 = JobSearchProfile(experience={"excluded_seniority": ["senior"]})
    cfg = build_filter_config(v1, ENGINE)
    assert cfg["career_level"] is None and "senior" in cfg["seniority"]["exclude"]
    gate = HardConstraintGate(user_max_education_level=None, career_level=cfg["career_level"])
    assert "seniority" not in gate.check(JobRequirements(), _job("Senior dev")).states


def test_v2_profile_with_targets_turns_the_title_keyword_rejection_off():
    v2 = _profile(["junior"], experience={"excluded_seniority": ["senior", "lead"]})
    cfg = build_filter_config(v2, ENGINE)
    assert cfg["seniority"]["exclude"] == [] and cfg["career_level"]["max_rank"] == 2
    # v2 without targets keeps v1 behaviour
    assert build_filter_config(_profile([], experience={"excluded_seniority": ["senior"]}), ENGINE)[
        "career_level"] is None


def test_scoring_prefers_the_target_levels_of_a_v2_profile():
    from src.pipeline.scoring import build_scoring_config

    v1 = JobSearchProfile(experience={"preferred_levels": ["junior", "entry_level", "graduate"]})
    v2 = _profile(["graduate", "junior"], experience={"preferred_levels": ["senior"]})
    kw = lambda p: set(build_scoring_config({}, p, ENGINE)["scoring"]["seniority_match"]["preferred_level_keywords"])  # noqa: E731
    assert kw(v2) == kw(v1) == {"junior", "debutant", "entry level", "graduate"}
