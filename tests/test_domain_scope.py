"""A profile's own line of work is never "another line of work" (src/pipeline/domain_scope.py).

``excluded_title_keywords`` / ``non_it_title_functions`` in config/filters.yaml hold title
words of lines of work. They must reject neither the jobs a nurse searches for because
they are nursing jobs, nor change anything for an IT profile.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.discovery.presets import load_presets
from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.domain_scope import groups_outside_profile, profile_terms
from src.pipeline.filter import build_filter_config, load_filters, matches_title_keywords
from src.pipeline.scoring import build_scoring_config, load_scoring_config

CONFIG = Path(__file__).parent.parent / "config"
FILTERS = load_filters(str(CONFIG / "filters.yaml"))
SECTIONS = ("excluded_title_keywords", "non_it_title_functions")
_RECENT = datetime.now(UTC) - timedelta(days=1)

IT_DOMAINS = sorted(next(p for p in load_presets() if p.name == "it").domains)


def _flat(section: str) -> list[str]:
    return [kw for group in FILTERS[section].values() for kw in group]


def _job(title: str) -> Job:
    return Job(source="test", title=title, company="Acme", published_at=_RECENT,
               remote=False, location="Paris, France", countries=["France"])


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize("domain", IT_DOMAINS)
def test_every_it_domain_keeps_every_group(domain: str, section: str) -> None:
    """Parity: an IT profile gets the same vocabulary as before this change."""
    profile = JobSearchProfile(target_domains=[domain], target_skills=["python", "docker", "git"])
    assert groups_outside_profile(FILTERS[section], profile) == _flat(section)


def test_the_tracked_example_profile_keeps_every_group() -> None:
    from src.pipeline.filter import load_profile

    profile = load_profile(str(CONFIG / "profile.yaml"))
    for section in SECTIONS:
        assert groups_outside_profile(FILTERS[section], profile) == _flat(section)


@pytest.mark.parametrize("domain", ["infirmier", "Infirmière", "aide soignant", "Aide-soignante", "healthcare"])
def test_a_nurse_domain_lifts_the_healthcare_group_only(domain: str) -> None:
    profile = JobSearchProfile(target_domains=[domain])
    kept = groups_outside_profile(FILTERS["excluded_title_keywords"], profile)
    assert "infirmier" not in kept and "médecin" not in kept
    assert "comptable" in kept and "juriste" in kept


def test_an_occupation_lifts_its_group_in_either_language() -> None:
    french = JobSearchProfile(target_occupations=[
        {"label": "nurse", "keyword": "nurse", "label_fr": "infirmier responsable des soins généraux"}])
    assert "infirmier" not in groups_outside_profile(FILTERS["excluded_title_keywords"], french)
    accountant = JobSearchProfile(target_occupations=[{"label": "accountant", "keyword": "comptable"}])
    assert "comptable" not in groups_outside_profile(FILTERS["excluded_title_keywords"], accountant)


def test_a_skill_alone_does_not_lift_a_group() -> None:
    profile = JobSearchProfile(target_domains=["software_development"], target_skills=["comptable"])
    assert "comptable" in groups_outside_profile(FILTERS["excluded_title_keywords"], profile)


def test_whole_words_only() -> None:
    profile = JobSearchProfile(target_domains=["commercialisation"])
    assert "commercial" in groups_outside_profile(FILTERS["non_it_title_functions"], profile)
    sales = JobSearchProfile(target_domains=["commercial"])
    assert "commercial" not in groups_outside_profile(FILTERS["non_it_title_functions"], sales)


def test_terms_are_normalized_and_deduplicated() -> None:
    profile = JobSearchProfile(target_domains=["Infirmière", "infirmiere", "Aide-Soignante"])
    assert profile_terms(profile) == ["infirmiere", "aide soignante"]


def test_a_nurse_profile_accepts_nursing_jobs_in_the_title_filter() -> None:
    config = build_filter_config(JobSearchProfile(target_domains=["infirmier"]), FILTERS)
    assert matches_title_keywords(_job("Infirmier DE (H/F)"), config)
    assert "infirmier" not in config["title_keywords"]["exclude"]
    assert "comptable" in config["title_keywords"]["exclude"]


def test_an_it_profile_still_rejects_another_line_of_work_in_the_title_filter() -> None:
    config = build_filter_config(JobSearchProfile(target_domains=["software_development"]), FILTERS)
    assert not matches_title_keywords(_job("Infirmier développeur"), config)
    assert not matches_title_keywords(_job("Commercial informatique"), config)


def test_scoring_off_domain_vocabulary_leaves_out_the_users_own_work() -> None:
    nurse = JobSearchProfile(target_domains=["infirmier"])
    cfg = build_scoring_config(load_scoring_config(str(CONFIG / "scoring.yaml")), nurse, FILTERS,
                               esco_synonyms={})
    off = cfg["scoring"]["domain_relevance"]["off_domain_keywords"]
    assert "infirmier" not in off and "comptable" in off
    it = JobSearchProfile(target_domains=["software_development"])
    it_cfg = build_scoring_config(load_scoring_config(str(CONFIG / "scoring.yaml")), it, FILTERS,
                                  esco_synonyms={})
    assert "infirmier" in it_cfg["scoring"]["domain_relevance"]["off_domain_keywords"]
