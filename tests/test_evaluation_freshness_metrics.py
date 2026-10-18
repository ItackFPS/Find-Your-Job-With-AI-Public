"""Rejection-reason breakdown and the freshness diagnostic (src/evaluation/metrics.py)."""

from __future__ import annotations

from src.evaluation.metrics import (
    DIAGNOSTIC_WARNING, FRESHNESS_REASON, diagnostic_excluding_freshness, evaluate, rejection_reason,
    rejections_by_reason,
)
from src.evaluation.models import Decision, LabelRecord


def _label(jid: str, expected: str) -> LabelRecord:
    return LabelRecord(job_id=jid, expected=expected, label_source="human", labeler="t", labeled_at="2026-01-01")


def _acc(jid: str) -> Decision:
    return Decision(job_id=jid, verdict="accept", stage="passed")


def _rej(jid: str, reason: str, stage: str = "filter") -> Decision:
    return Decision(job_id=jid, verdict="reject", stage=stage, reasons=[reason])


def _fixture():
    decisions = [
        _acc("tp"),                                             # accept / accepted
        _rej("fn_age", FRESHNESS_REASON),                       # accept / rejected for age
        _rej("fn_sen", "filter:seniority"),                     # accept / rejected for another reason
        _acc("fp"),                                             # reject / accepted
        _rej("tn_age1", FRESHNESS_REASON),                      # reject / rejected for age
        _rej("tn_age2", FRESHNESS_REASON),
        _rej("tn_loc", "filter:location"),                      # reject / rejected for location
        _rej("tn_gate", "Job requires 5 years", stage="requirements_gate"),
        _rej("bd_age", FRESHNESS_REASON),                       # borderline / rejected for age
        _rej("unl", FRESHNESS_REASON),                          # no label at all
    ]
    labels = {lab.job_id: lab for lab in [
        _label("tp", "accept"), _label("fn_age", "accept"), _label("fn_sen", "accept"), _label("fp", "reject"),
        _label("tn_age1", "reject"), _label("tn_age2", "reject"), _label("tn_loc", "reject"),
        _label("tn_gate", "reject"), _label("bd_age", "borderline"),
    ]}
    return decisions, labels


def test_rejection_reason_is_the_first_failing_filter_or_the_stage():
    assert rejection_reason(_acc("a")) is None
    assert rejection_reason(_rej("a", "filter:seniority")) == "filter:seniority"
    assert rejection_reason(_rej("a", "free text from the gate", stage="requirements_gate")) == "requirements_gate"


def test_rejections_by_reason_splits_each_reason_by_human_label():
    decisions, labels = _fixture()
    got = rejections_by_reason(decisions, labels)
    assert list(got) == sorted(got)                                       # deterministic order
    assert got[FRESHNESS_REASON] == {"total": 5, "by_label": {"accept": 1, "borderline": 1, "reject": 2, "unlabelled": 1}}
    assert got["filter:seniority"] == {"total": 1, "by_label": {"accept": 1}}
    assert got["requirements_gate"] == {"total": 1, "by_label": {"reject": 1}}
    assert sum(b["total"] for b in got.values()) == sum(1 for d in decisions if d.verdict == "reject")


def test_the_diagnostic_removes_only_published_age_rejections_and_is_labelled_not_comparable():
    decisions, labels = _fixture()
    diag = diagnostic_excluding_freshness(decisions, labels)
    assert diag["kind"] == "diagnostic_not_comparable" and diag["warning"] == DIAGNOSTIC_WARNING
    assert "NOT comparable" in diag["warning"] and "different evaluation population" in diag["warning"]
    assert diag["excluded_reason"] == FRESHNESS_REASON
    assert diag["n_excluded"] == 5 and diag["n_jobs"] == 5
    assert diag["excluded_by_label"] == {"accept": 1, "borderline": 1, "reject": 2, "unlabelled": 1}
    o = diag["overall"]
    assert (o["tp"], o["fn"], o["fp"], o["tn"], o["borderline"]) == (1, 1, 1, 2, 0)


def test_default_metrics_are_unchanged_and_independent_of_the_diagnostic():
    decisions, labels = _fixture()
    m = evaluate(decisions, labels)
    o = m["overall"]
    assert (o["tp"], o["fn"], o["fp"], o["tn"], o["borderline"]) == (1, 2, 1, 4, 1)      # the full population
    assert m["false_negative_ids"] == ["fn_age", "fn_sen"] and m["false_positive_ids"] == ["fp"]
    assert m["rejections_by_stage"] == {"filter": 7, "requirements_gate": 1}
    assert m["n_jobs"] == 10 and m["n_labelled"] == 9 and m["n_unlabelled"] == 1
    assert m["diagnostic_excluding_freshness"]["overall"] != o


def test_the_diagnostic_is_empty_but_well_formed_when_nothing_is_age_rejected():
    decisions = [_acc("a"), _rej("b", "filter:location")]
    labels = {lab.job_id: lab for lab in [_label("a", "accept"), _label("b", "reject")]}
    diag = diagnostic_excluding_freshness(decisions, labels)
    assert diag["n_excluded"] == 0 and diag["excluded_by_label"] == {}
    assert diag["overall"]["tp"] == 1 and diag["overall"]["tn"] == 1
