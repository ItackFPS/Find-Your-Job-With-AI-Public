"""An offer known only by an extract is not scored as if the extract were complete."""

from __future__ import annotations

from src.models.job import Job
from src.pipeline.scoring import _compute_factors, _score_skills_match

CONFIG = {"scoring": {"skills_match": {"target_skills": ["Linux", "DNS"]},
                      "domain_relevance": {"domain_title_keywords": ["infirmier"], "off_domain_keywords": ["vendeur"]}}}


def _job(title: str = "Agent polyvalent", description: str = "Extrait court...", **payload: object) -> Job:
    return Job(id="j", source="france-travail-web", title=title, description=description,
               canonical_url="https://x/1", raw_payload=payload)


def test_no_skill_in_an_extract_is_unknown_not_absent() -> None:
    assert _score_skills_match(_job(description_complete=False), CONFIG) == 0.5


def test_no_skill_in_a_full_description_stays_a_mismatch() -> None:
    assert _score_skills_match(_job(), CONFIG) == 0.05


def test_a_skill_found_in_an_extract_still_counts() -> None:
    assert _score_skills_match(_job(description="Linux en production", description_complete=False), CONFIG) > 0.5


def test_source_occupation_counts_as_domain_evidence() -> None:
    job = _job(description_complete=False, occupation={"code": "1", "named_domains": ["Soins"]})
    assert _compute_factors(job, CONFIG)["domain_relevance"] == 0.7


def test_an_occupation_sharing_one_word_with_the_domain_is_not_evidence() -> None:
    job = _job(description_complete=False, occupation={"code": "1", "target_domains": ["Soins"], "named_domains": []})
    assert _compute_factors(job, CONFIG)["domain_relevance"] == 0.45


def test_source_occupation_never_rescues_a_confirmed_mismatch() -> None:
    job = _job(title="Vendeur", description_complete=False, occupation={"code": "1", "named_domains": ["Soins"]})
    assert _compute_factors(job, CONFIG)["domain_relevance"] == 0.05


def test_without_occupation_evidence_the_domain_is_unchanged() -> None:
    assert _compute_factors(_job(description_complete=False), CONFIG)["domain_relevance"] == 0.45
