"""Ranking metrics: how well the score orders the jobs the gates let through (hand-computed)."""

from __future__ import annotations

import pytest

from src.evaluation.metrics import evaluate, ranking_delta, ranking_metrics
from src.evaluation.models import Decision, LabelRecord


def _label(jid: str, expected: str) -> LabelRecord:
    return LabelRecord(job_id=jid, expected=expected, label_source="human", labeler="t", labeled_at="2026-01-01")


def _dec(jid: str, verdict: str, score: float | None) -> Decision:
    return Decision(job_id=jid, verdict=verdict, stage="passed" if verdict == "accept" else "filter", score=score)


LABELS = {j: _label(j, e) for j, e in
          [("a1", "accept"), ("a2", "accept"), ("r1", "reject"), ("r2", "reject"), ("r3", "reject"),
           ("b1", "borderline"), ("x", "accept")]}


def test_auc_precision_at_k_and_the_visibility_floor():
    decisions = [_dec("a1", "accept", 0.80), _dec("r1", "accept", 0.70), _dec("a2", "accept", 0.65),
                 _dec("b1", "accept", 0.62), _dec("r2", "accept", 0.50), _dec("r3", "accept", 0.65),
                 _dec("x", "reject", None), _dec("unlabelled", "accept", 0.99)]
    m = ranking_metrics(decisions, LABELS)
    # pairs (accept, reject) among accepted: a1 beats r1 r2 r3; a2 beats r2, ties r3 (0.5), loses to r1
    assert m["auc"] == pytest.approx((3 + 1.5) / 6)
    assert (m["n_accept"], m["n_reject"], m["n_borderline"]) == (2, 3, 1)
    # labelled accepted jobs by score: a1 .80, r1 .70, a2 .65 / r3 .65 (tie: job id), b1 .62, r2 .50
    assert m["precision_at"]["5"] == pytest.approx(2 / 5)
    assert m["precision_at"]["10"] is None                       # fewer than k labelled jobs shown
    floor = m["visible_at_floor"]
    assert floor["tier"] == "relevant" and floor["min_score"] == 0.6
    assert (floor["accept"], floor["borderline"], floor["reject"]) == (2, 1, 2)
    assert floor["accept_hidden"] == 0
    # a labelled accept the gates rejected is never shown, whatever its score
    assert m["accept_rejected_by_gates"] == 1
    # sensitivity: borderline counted as accept (b1 .62 beats r2 only, ties none): (4.5 + 1) / 9
    assert m["auc_borderline_as_accept"] == pytest.approx((4.5 + 1) / 9)


def test_every_ranking_states_its_population_and_labels():
    d = ranking_metrics([], LABELS)["definition"]
    assert "accepted by the gates" in d["population"] and d["positive"] == "accept" and d["negative"] == "reject"
    assert "borderline" in d["excluded"] and "unlabelled" in d["excluded"] and "higher" in d["direction"]


def test_undefined_rankings_are_none_not_zero():
    m = ranking_metrics([_dec("a1", "accept", 0.8)], LABELS)
    assert m["auc"] is None and m["precision_at"]["5"] is None
    assert ranking_metrics([], {})["auc"] is None


def test_evaluate_reports_ranking_beside_the_gate_metrics():
    out = evaluate([_dec("a1", "accept", 0.8), _dec("r1", "accept", 0.7)], LABELS)
    assert out["ranking"]["auc"] == 1.0


def test_ranking_delta_bootstraps_the_paired_auc_difference():
    base = [_dec("a1", "accept", 0.6), _dec("a2", "accept", 0.6), _dec("r1", "accept", 0.7),
            _dec("r2", "accept", 0.7), _dec("r3", "accept", 0.5)]
    cand = [_dec("a1", "accept", 0.9), _dec("a2", "accept", 0.8), _dec("r1", "accept", 0.7),
            _dec("r2", "accept", 0.7), _dec("r3", "accept", 0.5)]
    d = ranking_delta(base, cand, LABELS)
    assert d["auc"] == pytest.approx(1.0 - 2 / 6)
    lo, hi = d["auc_diff_ci95"]
    assert lo <= d["auc"] <= hi and lo >= 0                       # the candidate never ranks worse here
    assert d == ranking_delta(base, cand, LABELS)                 # deterministic (fixed seed)
    assert ranking_delta(base, base, LABELS)["auc"] == 0


def test_the_delta_compares_one_cohort_not_two_populations():
    """A candidate whose gates drop a badly ranked reject changes its own AUC without ranking any better."""
    base = [_dec("a1", "accept", 0.6), _dec("a2", "accept", 0.5), _dec("r1", "accept", 0.4),
            _dec("r2", "accept", 0.9)]
    cand = [_dec("a1", "accept", 0.6), _dec("a2", "accept", 0.5), _dec("r1", "accept", 0.4),
            _dec("r2", "reject", None)]
    d = ranking_delta(base, cand, LABELS)
    assert d["auc"] == 0                                           # same order on the jobs both accept
    assert d["cohort"] == {"common": 3, "only_baseline": 1, "only_candidate": 0}
    assert ranking_metrics(cand, LABELS)["auc"] - ranking_metrics(base, LABELS)["auc"] > 0   # the trap


def test_the_delta_lists_the_jobs_that_moved_most():
    base = [_dec("a1", "accept", 0.5), _dec("r1", "accept", 0.9), _dec("r2", "accept", 0.7)]
    cand = [_dec("a1", "accept", 0.95), _dec("r1", "accept", 0.9), _dec("r2", "accept", 0.7)]
    [top, *_] = ranking_delta(base, cand, LABELS)["moved"]
    assert top == {"job_id": "a1", "expected": "accept", "rank": [3, 1], "score": [0.5, 0.95]}


def test_ranking_delta_with_too_few_labels_says_so():
    d = ranking_delta([_dec("a1", "accept", 0.6)], [_dec("a1", "accept", 0.9)], LABELS)
    assert d["auc"] is None and d["auc_diff_ci95"] is None


def test_the_markdown_report_shows_the_ranking():
    from pathlib import Path

    from src.evaluation.dataset import load_dataset
    from src.evaluation.models import StrategyRef
    from src.evaluation.report import render_markdown
    from src.evaluation.runner import run_evaluation

    root = Path(__file__).resolve().parents[1]
    ds = load_dataset(root / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50",
                      require_complete_labels=False)
    md = render_markdown(run_evaluation(ds, StrategyRef(config_dir=str(root / "config"))))
    assert "## Ranking of the accepted jobs" in md and "at the `relevant` floor" in md


def test_precision_at_strong_recall_at_20_and_the_auc_interval():
    decisions = [_dec("a1", "accept", 0.80), _dec("r1", "accept", 0.70), _dec("a2", "accept", 0.65),
                 _dec("b1", "accept", 0.62), _dec("r2", "accept", 0.50), _dec("r3", "accept", 0.64),
                 _dec("x", "reject", None)]
    m = ranking_metrics(decisions, LABELS)
    strong = m["precision_at_strong"]                            # strong tier: score >= 0.65
    assert (strong["tier"], strong["min_score"], strong["n"], strong["accept"]) == ("strong", 0.65, 3, 2)
    assert strong["value"] == pytest.approx(2 / 3) and strong["ci95"][0] < 2 / 3 < strong["ci95"][1]
    recall = m["recall_at_20"]                                   # x is accept but gate-rejected: never in the top
    assert (recall["k"], recall["n_accept"], recall["in_top"]) == (20, 3, 2)
    assert recall["value"] == pytest.approx(2 / 3)
    lo, hi = m["auc_ci95"]
    assert 0 <= lo <= m["auc"] <= hi <= 1
    assert ranking_metrics(decisions, LABELS)["auc_ci95"] == (lo, hi)      # seeded: reproducible


def test_intervals_are_absent_rather_than_invented_without_evidence():
    m = ranking_metrics([_dec("a1", "accept", 0.3)], LABELS)
    assert m["auc_ci95"] is None
    assert m["precision_at_strong"]["value"] is None and m["precision_at_strong"]["ci95"] is None
    assert m["recall_at_20"]["in_top"] == 1
