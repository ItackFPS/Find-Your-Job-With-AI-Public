"""Phase B B3-M metrics on synthetic cases with known values: false acceptance rate, NDCG@10, coverage
by domain, and the difference report with its "not measurable" verdict."""

from __future__ import annotations

import math

import pytest

from src.evaluation.metrics import (
    NOT_MEASURABLE, Confusion, confusion, coverage_by_domain, difference_report, ndcg_at,
)
from src.evaluation.models import Decision


def _d(job_id: str, verdict: str, score: float | None = None) -> Decision:
    return Decision(job_id=job_id, verdict=verdict, stage="passed" if verdict == "accept" else "filter",  # type: ignore[arg-type]
                    score=score)


def test_false_acceptance_rate() -> None:
    c = confusion([("reject", "accept"), ("reject", "reject"), ("reject", "reject"), ("reject", "reject"),
                   ("accept", "accept")])
    assert c.false_acceptance_rate == pytest.approx(0.25)
    assert c.to_dict()["false_acceptance_rate"] == pytest.approx(0.25)
    assert Confusion().false_acceptance_rate is None


def test_ndcg_known_values() -> None:
    assert ndcg_at(["accept", "accept", "reject"]) == pytest.approx(1.0)
    assert ndcg_at([]) is None and ndcg_at(["reject", "reject"]) is None
    # one accept at rank 2 of 2: (1/log2(3)) / (1/log2(2))
    assert ndcg_at(["reject", "accept"]) == pytest.approx(1 / math.log2(3))
    # borderline counts half; only the top k counts
    assert ndcg_at(["borderline", "accept"]) == pytest.approx((0.5 + 1 / math.log2(3)) / (1 + 0.5 / math.log2(3)))
    assert ndcg_at(["reject"] * 10 + ["accept"], k=10) == pytest.approx(0.0)


def test_ndcg_is_in_the_ranking_metrics() -> None:
    from src.evaluation.metrics import ranking_metrics
    from src.evaluation.models import LabelRecord

    decisions = [_d("a", "accept", 0.9), _d("b", "accept", 0.8), _d("c", "accept", 0.7)]
    labels = {j: LabelRecord(job_id=j, expected=e, label_source="human", labeler="t", labeled_at="2026-11-14T00:00:00+00:00")
              for j, e in (("a", "reject"), ("b", "accept"), ("c", "accept"))}
    r = ranking_metrics(decisions, labels)["ndcg_at_10"]
    assert r["n"] == 3 and r["value"] == pytest.approx(
        (1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3)))


def test_coverage_by_domain() -> None:
    decisions = [_d("1", "accept"), _d("2", "reject"), _d("3", "accept"), _d("4", "reject")]
    expected = {"1": "accept", "2": "accept", "3": "reject"}
    out = coverage_by_domain(decisions, expected, {"1": ["réseaux"], "2": ["réseaux"], "3": ["santé"]},
                             ["réseaux", "santé", "logistique"])
    assert out["réseaux"] == {"offers": 2, "accepted": 1, "labelled_accept": 2, "recall": 0.5,
                              "recall_ci95": out["réseaux"]["recall_ci95"]}
    assert out["santé"]["labelled_accept"] == 0 and out["santé"]["recall"] is None
    assert out["logistique"]["offers"] == 0


def test_difference_report_and_the_minimum_sample_rule() -> None:
    ids = [str(i) for i in range(40)]
    expected = {j: ("accept" if int(j) < 20 else "reject") for j in ids}
    base = [_d(j, "accept" if int(j) < 15 or int(j) >= 35 else "reject") for j in ids]
    cand = [_d(j, "accept" if int(j) < 19 or int(j) >= 35 else "reject") for j in ids]
    r = difference_report(base, cand, expected, effect={"recall": 0.05, "false_acceptance_rate": 0.9})
    assert r["n"] == 40
    assert r["recall"]["baseline"] == pytest.approx(0.75) and r["recall"]["candidate"] == pytest.approx(0.95)
    assert r["recall"]["difference"] == pytest.approx(0.20)
    lo, hi = r["recall"]["ci95"]
    assert lo <= 0.20 <= hi
    # 20 labelled accepts cannot resolve a 0.05 effect: never reported as "no effect"
    assert r["recall"]["measurable"] is False and r["recall"]["verdict"] == NOT_MEASURABLE
    assert r["false_acceptance_rate"]["difference"] == pytest.approx(0.0)
    assert r["false_acceptance_rate"]["measurable"] is True
    assert r["gate_mcnemar"] == {"only_baseline_accepts": 0, "only_candidate_accepts": 4,
                                 "p": r["gate_mcnemar"]["p"]}
    assert r["gate_mcnemar"]["p"] == pytest.approx(0.125)


def test_difference_report_is_deterministic() -> None:
    ids = [str(i) for i in range(30)]
    expected = {j: ("accept" if int(j) % 2 else "reject") for j in ids}
    base = [_d(j, "accept" if int(j) % 3 else "reject") for j in ids]
    cand = [_d(j, "accept" if int(j) % 5 else "reject") for j in ids]
    assert difference_report(base, cand, expected) == difference_report(base, cand, expected)
