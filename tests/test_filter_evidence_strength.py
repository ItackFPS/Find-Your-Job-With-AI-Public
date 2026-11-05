"""Evidence strength of failed hard-filter checks (ADR 0019 v5 AD-2b; stage L5.1). Descriptive only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.decisions import DECISION_SCHEMA_VERSION
from src.decisions import evidence as ev
from src.decisions.models import EvidenceItem
from src.models.job import Job
from src.pipeline import filter as F


def _cfg(**over):
    base = {"locations": {"include": ["france"]}, "remote": {"allowed": []}, "work_modes": {"allowed": ["remote"]},
            "seniority": {"exclude": ["director"]}, "contract_type": {"exclude_title_keywords": ["freelance"]},
            "title_keywords": {"include": ["python"]}, "posted_within_days": 30, "salary_requirements": {}}
    base.update(over)
    return base


OLD = datetime.now(UTC) - timedelta(days=90)


@pytest.mark.parametrize("code,job,cfg,want", [
    ("location", Job(id="j", title="Python dev", countries=["Germany"]), _cfg(), "strong"),
    ("work_mode", Job(id="j", title="Python dev", work_mode="on_site"), _cfg(), "strong"),         # structured
    ("work_mode", Job(id="j", title="Python dev", countries=["France"]), _cfg(), "weak"),          # inferred
    ("seniority", Job(id="j", title="Python director"), _cfg(), "weak"),
    ("published_age", Job(id="j", title="Python dev", published_at=OLD), _cfg(), "weak"),
    ("title_keywords", Job(id="j", title="Comptable"), _cfg(), "weak"),
    ("contract_type", Job(id="j", title="Python freelance"), _cfg(), "weak"),
    ("salary", Job(id="j", title="Python dev", salary_min=20000.0),
     _cfg(salary_requirements={"min_salary": 30000}), "weak"),
    ("salary", Job(id="j", title="Python dev", salary_min=20000.0),
     _cfg(salary_requirements={"min_salary": 30000, "strict": True}), "strong"),
])
def test_failed_checks_carry_the_ad2_strength(code, job, cfg, want):
    outcome = {o.code: o for o in F.explain_filter_checks(job, cfg)}[code]
    assert not outcome.passed and outcome.strength == want
    assert F.violation_strength(code, job, cfg) == want


def test_passed_checks_have_no_strength():
    job = Job(id="j", title="Python dev", countries=["France"], remote=True, published_at=datetime.now(UTC))
    outcomes = F.explain_filter_checks(job, _cfg())
    assert all(o.passed for o in outcomes) and {o.strength for o in outcomes} == {None}


@pytest.mark.parametrize("precision,want", [("commune", "strong"), ("department", "weak")])
def test_distance_is_strong_only_at_commune_precision(monkeypatch, precision, want):
    monkeypatch.setattr(F, "assess", lambda job, cfg: SimpleNamespace(place=SimpleNamespace(precision=precision)))
    assert F.violation_strength("distance", Job(id="j"), {"location_distance": {"home": {}}}) == want


def test_remote_policy_is_strong():
    assert F.violation_strength("remote_policy", Job(id="j"), {}) == "strong"


def test_strength_never_changes_the_filter_verdict():
    jobs = [Job(id=str(i), title=t, countries=c, published_at=p, remote=True) for i, (t, c, p) in enumerate([
        ("Python dev", ["France"], datetime.now(UTC)), ("Python director", [], None),
        ("Comptable", [], None), ("Python dev", ["Germany"], None), ("Python dev", [], OLD)])]
    valid, reasons = F.filter_jobs(jobs, _cfg())
    for job in jobs:
        first = [o for o in F.explain_filter_checks(job, _cfg(), stop_at_first_failure=True) if not o.passed]
        assert (first[0].code if first else None) == reasons.get(job.id)
    assert [j.id for j in valid] == ["0"]


def test_journal_items_record_the_strength():
    job = Job(id="j", title="Python director", published_at=OLD, countries=["Germany"])
    items = ev.filter_failure_items(job, _cfg(), "location", all_failing=True)
    got = {i.source.rule_id: i.strength for i in items}
    assert got["filter.location"] == "strong"
    assert got["filter.seniority"] == "weak" and got["filter.published_age"] == "weak"
    passed = ev.filter_pass_items(Job(id="k", title="Python dev", countries=["France"]), _cfg())
    assert {i.strength for i in passed} == {None}


def test_schema_2_is_additive_and_reads_schema_1_items():
    assert DECISION_SCHEMA_VERSION == 2
    legacy = {"dimension": "location", "polarity": "negative", "state": "violated",
              "source": {"stage": "filter", "rule_id": "filter.location", "evidence_source": "rule"}}
    assert EvidenceItem.model_validate(legacy).strength is None
