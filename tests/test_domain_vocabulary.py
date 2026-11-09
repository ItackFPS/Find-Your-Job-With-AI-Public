"""A free-text line of work must reach the vocabulary, for any line of work."""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.domain_vocabulary import (
    effective_domains,
    resolve_domains,
    stem,
    title_has_stem_set,
)
from src.pipeline.filter import build_filter_config, matches_title_keywords
from src.pipeline.scoring import _score_domain_relevance, build_scoring_config

VOCAB = {
    "systems": {"title": ["administrateur systèmes", "technicien informatique"], "description": ["windows server"]},
    "it_support": {"title": ["support informatique", "help desk"], "description": []},
    "informatique": {"title": ["informatique", "informaticien", "développeur"], "description": []},
    "healthcare": {"title": ["infirmier", "médecin"], "description": []},
    "accounting": {"title": ["comptable", "auditeur"], "description": []},
    "cybersécurité": {"title": ["cybersécurité"], "description": ["siem"]},
}


@pytest.mark.parametrize(("a", "b"), [
    ("administration", "administrateur"), ("systèmes".replace("è", "e"), "systeme"),
    ("reseaux", "reseau"), ("informatique", "informaticien"), ("comptabilite", "comptable"),
    ("infirmiers", "infirmiere"), ("boulangerie", "boulanger"),
])
def test_stem_joins_the_forms_of_a_word(a: str, b: str) -> None:
    assert stem(a) == stem(b)


def test_stem_keeps_information_apart_from_informatique() -> None:
    assert stem("information") != stem("informatique")


def test_exact_key_takes_the_whole_entry_ignoring_case_and_accents() -> None:
    v = resolve_domains(["CYBERSECURITE"], VOCAB)
    assert v.families == ("cybersécurité",)
    assert "siem" in v.description_terms


def test_label_naming_a_keyword_takes_the_whole_entry() -> None:
    v = resolve_domains(["Administration systèmes"], VOCAB)
    assert "systems" in v.families
    assert "technicien informatique" in v.title_terms  # a sibling keyword of the named one


def test_single_word_label_does_not_pull_its_neighbours() -> None:
    v = resolve_domains(["Infirmier"], VOCAB)
    assert "infirmier" in v.title_terms
    assert "médecin" not in v.title_terms


def test_partial_label_takes_only_the_keywords_it_contains() -> None:
    v = resolve_domains(["Soins infirmiers"], VOCAB)
    assert v.families == ()
    assert "infirmier" in v.title_terms and "médecin" not in v.title_terms


def test_broad_word_of_a_label_is_taken_as_a_keyword() -> None:
    v = resolve_domains(["Maintenance informatique"], VOCAB)
    assert "informatique" in v.title_terms and "développeur" not in v.title_terms


def test_unknown_label_stands_for_itself() -> None:
    v = resolve_domains(["Apiculture biologique"], VOCAB)
    assert v.families == ()
    assert v.title_terms == ("Apiculture biologique",)
    assert title_has_stem_set("Apiculteur en production biologique", v.title_stem_sets)
    assert not title_has_stem_set("Apiculteur", v.title_stem_sets)


def test_stem_set_ignores_word_order_and_inflection() -> None:
    v = resolve_domains(["Administration systèmes"], {})
    assert title_has_stem_set("Systèmes : administrateur H/F", v.title_stem_sets)


def _profile(**kw) -> JobSearchProfile:
    return JobSearchProfile(**kw)


ENGINE = {"domain_keywords": VOCAB, "excluded_title_keywords": {}, "non_it_title_functions": {}}


def _job(title: str, description: str = "") -> Job:
    return Job(id="j", source="test", title=title, description=description, canonical_url="https://x/1")


def test_scoring_reads_a_typed_domain() -> None:
    config = build_scoring_config({}, _profile(target_domains=["Support informatique"]), ENGINE, esco_synonyms={})
    assert _score_domain_relevance("Technicien help desk", "", config) == 1.0


def test_scoring_reads_a_typed_domain_of_another_line_of_work() -> None:
    config = build_scoring_config({}, _profile(target_domains=["Soins infirmiers"]), ENGINE, esco_synonyms={})
    assert _score_domain_relevance("Infirmière de bloc (H/F)", "", config) == 1.0
    assert _score_domain_relevance("Boulanger", "", config) == 0.45


def test_scoring_title_stem_set_counts_as_a_title_match() -> None:
    config = build_scoring_config({}, _profile(target_domains=["Gestion paie"]), ENGINE, esco_synonyms={})
    assert _score_domain_relevance("Gestionnaire de paie H/F", "", config) == 1.0


def test_scoring_description_names_the_typed_domain() -> None:
    config = build_scoring_config({}, _profile(target_domains=["Gestion paie"]), ENGINE, esco_synonyms={})
    assert _score_domain_relevance("Assistant RH", "Vous assurez la gestion paie.", config) == 0.7


@pytest.mark.parametrize("title", ["Technicien / Technicienne de help desk", "Support Informatique N1"])
def test_hard_filter_keeps_titles_of_a_typed_domain(title: str) -> None:
    cfg = build_filter_config(_profile(target_domains=["Support informatique"], focus_domain=True), ENGINE)
    assert matches_title_keywords(_job(title), cfg)


def test_hard_filter_title_stem_set() -> None:
    cfg = build_filter_config(_profile(target_domains=["Gestion paie"], focus_domain=True), ENGINE)
    assert matches_title_keywords(_job("Gestionnaire de paie"), cfg)
    assert not matches_title_keywords(_job("Gestionnaire de stock"), cfg)


def test_effective_domains_keeps_labels_first() -> None:
    profile = _profile(target_domains=["Administration systèmes"])
    assert effective_domains(profile, VOCAB) == ["Administration systèmes", "systems"]
