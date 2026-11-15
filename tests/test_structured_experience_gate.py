"""France Travail's typed experience field in the requirements gate (Phase B B6-A, owner decision D12).

With max_years = M: more than M + 1 years required → excluded; more than M and up to M + 1 →
kept with a lower score; M or less → normal. Missing, malformed or contradictory data never rejects.
"""

from __future__ import annotations

import pytest

from src.requirements.experience import extract_experience
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import ConstraintState, JobRequirements


class _Job:
    def __init__(self, raw: object, source: str = "france-travail", jid: str = "j") -> None:
        self.id, self.source, self.title, self.description, self.raw_payload = jid, source, "Technicien", "", raw


def _ft(code: object, label: object) -> dict:
    return {"_france_travail_raw": {"experienceExige": code, "experienceLibelle": label}}


def _run(job: _Job, text: str = "", max_years: int = 2):
    req = JobRequirements(experience=extract_experience(text) if text else None)
    gate = HardConstraintGate(user_max_education_level=None, user_max_years_experience=max_years)
    passing, results = gate_jobs([job], {job.id: req}, gate)
    return bool(passing), results[job.id]


def test_more_than_max_plus_one_years_required_by_the_field_is_excluded() -> None:
    passed, result = _run(_Job(_ft("E", "5 An(s)")))
    assert not passed
    reason = next(r for r in result.reasons if r.type == "experience")
    assert reason.evidence_source.value == "structured_field" and "5" in reason.requirement


@pytest.mark.parametrize("label", ["3 An(s)", "2 An(s) 6 Mois", "30 Mois"])
def test_within_one_year_above_the_max_is_kept_with_a_lower_score(label: str) -> None:
    passed, result = _run(_Job(_ft("E", label)))
    assert passed and result.experience_penalty > 0
    assert any("lower score" in f for f in result.flags)
    assert result.min_experience_years == 3


@pytest.mark.parametrize("label", ["1 An(s)", "2 An(s)", "6 Mois"])
def test_up_to_the_max_is_ranked_normally(label: str) -> None:
    passed, result = _run(_Job(_ft("E", label)))
    assert passed and result.experience_penalty == 0
    assert result.min_experience_years is not None   # experience fit is known, not unknown


def test_a_beginner_offer_is_known_and_never_penalised() -> None:
    passed, result = _run(_Job(_ft("D", "Débutant accepté")))
    assert passed and result.experience_penalty == 0 and result.min_experience_years == 0


def test_a_required_experience_without_a_duration_applies_nothing() -> None:
    passed, result = _run(_Job(_ft("E", "Expérience exigée")))
    assert passed and result.experience_penalty == 0 and result.min_experience_years is None


@pytest.mark.parametrize("raw", [None, [], "x", {"_france_travail_raw": "x"}, _ft(42, None), _ft("E", 5),
                                 _ft("Z", "beaucoup"), _ft("", "")])
def test_missing_or_malformed_fields_never_reject(raw: object) -> None:
    passed, result = _run(_Job(raw))
    assert passed and result.experience_penalty == 0


def test_text_and_field_that_disagree_are_contradictory_and_never_reject() -> None:
    passed, result = _run(_Job(_ft("D", "Débutant accepté")), text="5 ans d'expérience exigés.")
    assert passed and result.experience_penalty == 0 and result.min_experience_years is None
    assert result.states["experience"] == ConstraintState.UNKNOWN
    assert any("contradictory" in f for f in result.flags)


def test_text_and_field_that_agree_use_the_field() -> None:
    passed, result = _run(_Job(_ft("E", "5 An(s)")), text="5 ans d'expérience exigés.")
    assert not passed
    assert next(r for r in result.reasons if r.type == "experience").evidence_source.value == "structured_field"


def test_the_field_is_only_read_from_france_travail() -> None:
    passed, result = _run(_Job(_ft("E", "5 An(s)"), source="indeed"))
    assert passed and result.min_experience_years is None


def test_the_field_at_the_top_level_of_the_payload_is_read_too() -> None:
    passed, _ = _run(_Job({"experienceExige": "E", "experienceLibelle": "6 An(s)"}))
    assert not passed
