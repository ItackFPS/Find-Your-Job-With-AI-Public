"""An offer known only by an extract is not scored as if the extract were complete (Phase B B5-F)."""

from __future__ import annotations

from src.models.job import Job
from src.pipeline.scoring import _score_skills_match

CONFIG = {"scoring": {"skills_match": {"target_skills": ["Linux", "DNS"]}}}


def _job(description: str = "Extrait court...", **payload: object) -> Job:
    return Job(id="j", source="france-travail-web", title="Agent polyvalent", description=description,
               url="https://example.test/1", company="Acme", location="Toulon", raw_payload=payload)


def test_no_skill_in_an_extract_is_unknown_not_absent() -> None:
    assert _score_skills_match(_job(description_complete=False), CONFIG) == 0.5


def test_no_skill_in_a_full_description_stays_a_mismatch() -> None:
    assert _score_skills_match(_job(), CONFIG) < 0.5
    assert _score_skills_match(_job(description_complete=True), CONFIG) < 0.5


def test_a_skill_found_in_an_extract_still_counts() -> None:
    assert _score_skills_match(_job(description="Linux en production", description_complete=False), CONFIG) > 0.5
