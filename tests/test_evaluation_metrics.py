"""Hand-computed checks for src/evaluation/metrics.py."""

from __future__ import annotations

import pytest

from src.evaluation.metrics import (
    Confusion, bootstrap_ci, confusion, evaluate, mcnemar_exact_p, percentile,
)
from src.evaluation.models import Decision, LabelRecord


def _label(jid: str, expected: str, category: str | None = None) -> LabelRecord:
    return LabelRecord(job_id=jid, expected=expected, category=category, label_source="human",
                       labeler="t", labeled_at="2026-01-01")


def _dec(jid: str, verdict: str, stage: str = "passed", flags: list[str] | None = None) -> Decision:
    return Decision(job_id=jid, verdict=verdict, stage=stage, flags=flags or [])


def test_confusion_counts_and_rates():
    c = confusion([
        ("accept", "accept"), ("accept", "accept"), ("accept", "reject"),   # tp tp fn
        ("reject", "reject"), ("reject", "accept"), ("reject", "reject"),   # tn fp tn
        ("borderline", "accept"),
    ])
    assert (c.tp, c.tn, c.fp, c.fn, c.borderline) == (2, 2, 1, 1, 1)
    assert c.accuracy == pytest.approx(4 / 6)
    assert c.precision == pytest.approx(2 / 3)
    assert c.recall == pytest.approx(2 / 3)
    assert c.f1 == pytest.approx(2 / 3)
    assert c.unsafe_rejection_rate == pytest.approx(1 / 3)
    assert c.reject_recall == pytest.approx(2 / 3)
    assert c.reject_precision == pytest.approx(2 / 3)


def test_empty_denominators_are_none_not_zero():
    c = Confusion()
    assert c.precision is None and c.recall is None and c.f1 is None
    assert c.unsafe_rejection_rate is None


def test_evaluate_joins_labels_after_the_fact_and_reports_ids():
    decisions = [_dec("a", "accept"), _dec("b", "reject", "filter"), _dec("c", "accept"), _dec("d", "reject", "requirements_gate")]
    labels = {lab.job_id: lab for lab in [_label("a", "accept", "x"), _label("b", "accept", "x"), _label("c", "reject", "y")]}
    m = evaluate(decisions, labels)
    assert m["n_labelled"] == 3 and m["n_unlabelled"] == 1
    assert m["false_negative_ids"] == ["b"] and m["false_positive_ids"] == ["c"]
    assert m["rejections_by_stage"] == {"filter": 1, "requirements_gate": 1}
    assert m["by_category"]["x"]["tp"] == 1 and m["by_category"]["x"]["fn"] == 1


def test_bootstrap_is_deterministic_for_a_seed_and_none_when_too_small():
    pairs = [("accept", "accept")] * 8 + [("accept", "reject")] * 2 + [("reject", "reject")] * 6 + [("reject", "accept")] * 2
    assert bootstrap_ci(pairs, seed=1) == bootstrap_ci(pairs, seed=1)
    ci = bootstrap_ci(pairs, seed=1)["recall"]
    assert ci["lo"] <= 0.8 <= ci["hi"]
    assert bootstrap_ci([("accept", "accept")])["recall"] == {"lo": None, "hi": None}


def test_mcnemar_exact_known_values():
    assert mcnemar_exact_p(0, 0) == 1.0
    assert mcnemar_exact_p(5, 5) == 1.0
    assert mcnemar_exact_p(0, 10) == pytest.approx(2 / 1024)
    assert mcnemar_exact_p(2, 8) == pytest.approx(2 * (1 + 10 + 45) / 1024)


def test_percentile():
    assert percentile([], 0.5) is None
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
