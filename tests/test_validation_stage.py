"""The validation stage's rules (Phase B B10, P-006): it rejects malformed offers that reach it."""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.pipeline.validate import validate_job, validate_jobs


def _job(**kwargs: object) -> Job:
    fields: dict[str, object] = {"title": "Comptable", "description": "Tenue des comptes."}
    fields.update(kwargs)
    return Job(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(("fields", "error"), [
    ({"title": "  "}, "Title is empty"),
    ({"company": ""}, "Company is explicitly empty"),
    ({"description": "", "location": None}, "Job lacks description, location, or salary information"),
])
def test_a_malformed_offer_is_rejected_with_its_reason(fields: dict, error: str) -> None:
    result = validate_job(_job(**fields))
    assert not result.is_valid and error in result.errors


@pytest.mark.parametrize("fields", [
    {},                                                    # title and description
    {"company": None},                                     # anonymous posting
    {"description": "", "location": "Lyon"},              # a location is enough
    {"description": "", "location": None, "salary_min": 30000},   # a salary is enough
])
def test_a_well_formed_offer_passes(fields: dict) -> None:
    assert validate_job(_job(**fields)).is_valid


def test_the_batch_keeps_only_valid_offers_and_reports_the_others() -> None:
    valid, errors = validate_jobs([_job(), _job(title=""), _job(company="")])
    assert len(valid) == 1 and errors == ["Title is empty", "Company is explicitly empty"]
