"""The domain and skills factors read past the employer's pitch (Phase B B5-D)."""

from __future__ import annotations

from src.models.job import Job
from src.pipeline.scoring import _DESCRIPTION_WINDOW, _score_domain_relevance, _score_skills_match

PITCH = "Notre entreprise familiale accompagne ses clients depuis trente ans. " * 45  # ~3,100 chars
DOMAIN = {"scoring": {"domain_relevance": {"domain_description_keywords": ["active directory"]}}}
SKILLS = {"scoring": {"skills_match": {"target_skills": ["Active Directory"]}}}


def _job(description: str) -> Job:
    return Job(id="w", source="test", url="https://example.test/w", title="Technicien",
               company="Acme", location="Toulon", description=description)


def test_a_domain_named_after_a_long_pitch_counts() -> None:
    assert len(PITCH) > 2000
    assert _score_domain_relevance("Technicien", PITCH + " Missions : Active Directory.", DOMAIN) == 0.7


def test_a_skill_named_after_a_long_pitch_counts() -> None:
    late = _score_skills_match(_job(PITCH + " Missions : Active Directory."), SKILLS)
    absent = _score_skills_match(_job(PITCH), SKILLS)
    assert late > absent


def test_the_window_is_bounded() -> None:
    far = "x" * (_DESCRIPTION_WINDOW + 10) + " active directory"
    assert _score_domain_relevance("Technicien", far, DOMAIN) != 0.7
