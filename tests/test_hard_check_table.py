"""The hard-filter checks are one table (``HARD_CHECKS``): the filter loop, the explain view and the
decision journal all read a check's verdict, strength, observed and expected from it."""

from __future__ import annotations

from src.decisions import evidence as ev
from src.models.job import Job
from src.pipeline import filter as F

CFG = {
    "locations": {"include": ["Paris"]},
    "seniority": {"exclude": ["stage"]},
    "posted_within_days": 30,
}


def test_every_journal_dimension_has_a_check_and_the_order_is_the_filter_order():
    codes = [c.code for c in F.HARD_CHECKS]
    assert set(codes) == set(ev.FILTER_DIMENSION)
    assert [code for code, _ in F._FILTER_CHECKS] == codes


def test_rankable_checks_derive_from_the_table_in_evaluation_order():
    assert F.RANKABLE_CHECKS == tuple(c.code for c in F.HARD_CHECKS if c.rankable)
    assert "salary" not in F.RANKABLE_CHECKS
    assert not {"location", "remote", "remote_policy"} & set(F.RANKABLE_CHECKS)


def test_unknown_code_is_weak_unobserved_and_unexpected():
    job = Job(id="j", title="Dev", source="x")
    assert F.violation_strength("nope", job, CFG) == "weak"
    assert F.filter_observed("nope", job, CFG) is None
    assert F.filter_expected("nope", CFG) is None
    assert F.weak_severity("nope", job, CFG) == 1.0


def test_journal_deciding_item_is_the_check_outcome():
    job = Job(id="j", title="Stage Python", source="x", remote=False)
    outcome = F.check_outcome("seniority", job, CFG, passed=False)
    (item,) = ev.filter_failure_items(job, CFG, "seniority", all_failing=False)
    assert (item.observed, item.expected, item.strength) == (
        outcome.observed, outcome.expected, outcome.strength)
    assert outcome.strength == "weak" and outcome.observed == "title='Stage Python'"


def test_passed_outcome_has_no_strength():
    job = Job(id="j", title="Dev", source="x")
    assert F.check_outcome("seniority", job, CFG, passed=True).strength is None
