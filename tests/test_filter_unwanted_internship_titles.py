"""A title that names an internship or an apprenticeship is rejected when the profile wants neither.

Found on human labels (real-v2, tuning split): for a profile asking CDI/CDD at graduate and
junior level, the first-ranked offer was "STAGE - Ingénieur Développeur Software". Only
freelance titles were excluded. The check stays title-only and whole-word, and is off for
a profile that accepts any contract or that names the contract or the level as wanted.

No Internet access. No collectors called. No real database touched.
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.filter import build_filter_config, matches_contract_type

ENGINE_CFG = {"contract_keywords": {
    "freelance": {"title": ["freelance"]},
    "internship": {
        "title": ["stage", "placement"],   # the matching list: never used to reject
        "reject_title_words": ["stage", "stagiaire", "intern", "internship"],
        "not_a_contract_phrases": ["early stage", "late stage", "1er stage", "premier stage"],
    },
    "alternance": {
        "title": ["alternance"],
        "reject_title_words": ["alternance", "alternant", "alternante", "apprenti", "apprentie",
                               "apprentissage", "apprenticeship", "apprentice"],
    },
}}


def _job(title: str) -> Job:
    return Job(source="test", title=title, company="Acme", remote=False,
               location="Toulon, France", countries=["France"])


def _passes(title: str, profile: JobSearchProfile) -> bool:
    return matches_contract_type(_job(title), build_filter_config(profile, ENGINE_CFG))


CDI_JUNIOR = JobSearchProfile(contract_types=["cdi", "cdd", "full-time"])


@pytest.mark.parametrize("title", [
    "STAGE - Ingénieur Développeur Software - H/F",
    "Stage Webmaster / Développeur Web (2 mois)",
    "Développeur web - stage - (F/H)",
    "Software Engineer (stage 6 mois)",
    "Stagiaire développeur informatique Java H/F",
    "Software Engineering Intern – Workflow Monitoring",
    "Data Platform Internship - Paris",
    "Alternance - Technicien support informatique",
    "Administrateur systèmes et réseaux en alternance (H/F)",
    "Alternant administrateur réseau",
    "Apprenti technicien de maintenance",
    "Data analyst apprentice",
])
def test_internship_and_apprenticeship_titles_are_rejected(title):
    assert _passes(title, CDI_JUNIOR) is False


@pytest.mark.parametrize("title", [
    "Administrateur Systèmes et Réseaux F/H",
    "Responsable communication interne",                  # "interne" is not "intern"
    "Ingénieur pipeline multi-stage CI/CD",               # a compound, not the contract
    "Product manager marketplace early stage H/F",
    "Ingénieur commercial (avec un 1er stage en prospection)",
    "Ingénieur expérimenté placement & routage de cartes",  # matching word only
    "Internal tools engineer",
])
def test_other_titles_are_kept(title):
    assert _passes(title, CDI_JUNIOR) is True


def test_a_profile_accepting_any_contract_rejects_nothing():
    assert _passes("STAGE - Ingénieur Développeur", JobSearchProfile(contract_types=[])) is True


def test_a_wanted_contract_is_kept():
    profile = JobSearchProfile(contract_types=["internship", "alternance", "cdi"])
    assert _passes("STAGE - Ingénieur Développeur", profile) is True
    assert _passes("Alternance - Technicien support", profile) is True
    # each contract is judged on its own
    only_internship = JobSearchProfile(contract_types=["internship", "full-time"])
    assert _passes("Stage développeur", only_internship) is True
    assert _passes("Alternance développeur", only_internship) is False


def test_a_target_career_level_counts_as_wanting_the_contract():
    # The profile page has no "alternance" contract box: the level is how it is asked for.
    profile = JobSearchProfile(profile_version=2, contract_types=["full-time"],
                               targets={"levels": ["alternance", "junior"]})
    assert _passes("Alternance - Technicien support", profile) is True
    assert _passes("Stage développeur", profile) is False


def test_no_reject_words_in_the_configuration_means_no_new_rejection():
    cfg = build_filter_config(CDI_JUNIOR, {"contract_keywords": {"freelance": {"title": ["freelance"]}}})
    assert cfg["contract_type"]["exclude_title_keywords"] == ["freelance"]
    assert matches_contract_type(_job("Stage développeur"), cfg) is True


def test_shipped_configuration_rejects_the_reported_title():
    from src.pipeline.filter import load_filters

    cfg = build_filter_config(CDI_JUNIOR, load_filters("config/filters.yaml"))
    assert matches_contract_type(_job("STAGE - Ingénieur Développeur Software - H/F"), cfg) is False
    assert matches_contract_type(_job("Administrateur Systèmes et DevOps (H/F)"), cfg) is True
