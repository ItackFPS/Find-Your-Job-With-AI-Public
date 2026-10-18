"""Shadow evaluation of the disagreement engine + review metrics."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.evaluation.__main__ import main as cli_main
from src.evaluation.dataset import load_dataset
from src.evaluation.metrics import review_metrics
from src.evaluation.models import Decision, LabelRecord, StrategyRef
from src.evaluation.runner import run_evaluation
from src.evaluation.store import read_golden, to_golden

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
GOLDEN = ROOT / "tests" / "fixtures" / "evaluation" / "golden" / "synthetic-ai-filter-50.json"


def _d(jid, verdict, state):
    return Decision(job_id=jid, verdict=verdict, stage="passed" if verdict == "accept" else "filter",
                    assessment={"state": state})


def _l(jid, expected):
    return LabelRecord(job_id=jid, expected=expected, label_source="human", labeler="t", labeled_at="x")


def test_review_metrics_hand_computed():
    ids = [f"j{i}" for i in range(40)]
    decisions, labels = [], {}
    for i, jid in enumerate(ids):
        wrong = i < 8                                     # 8 wrong decisions among 40
        decisions.append(_d(jid, "accept", "review" if (i < 6 or i in (20, 21)) else "accept"))   # 6 wrong + 2 right flagged
        labels[jid] = _l(jid, "reject" if wrong else "accept")
    m = review_metrics(decisions, labels)
    assert (m["n_labelled"], m["n_review"], m["n_wrong"], m["n_wrong_in_review"]) == (40, 8, 8, 6)
    assert m["review_rate"] == pytest.approx(0.2) and m["capture_rate"] == pytest.approx(0.75)
    assert m["lift"] == pytest.approx(3.75) and m["status"] == "promising"
    assert m["overall_accuracy"] == pytest.approx(0.8)
    assert m["auto_accuracy"] == pytest.approx((32 - 2) / 32)                      # 2 wrong left in auto (j6, j7)


def test_small_samples_never_yield_a_conclusion():
    decisions = [_d(f"j{i}", "accept", "review") for i in range(10)]
    labels = {f"j{i}": _l(f"j{i}", "reject") for i in range(10)}
    m = review_metrics(decisions, labels)
    assert m["status"] == "insufficient_evidence" and "no conclusion" in m["note"]
    assert review_metrics([], {})["status"] == "insufficient_evidence"


def test_unsafe_rejections_outside_review_are_counted_and_borderline_is_ignored():
    d = [_d("a", "reject", "reject"), _d("b", "reject", "review"), _d("c", "accept", "accept")]
    labels = {"a": _l("a", "accept"), "b": _l("b", "accept"), "c": _l("c", "borderline")}
    m = review_metrics(d, labels)
    assert m["n_labelled"] == 2 and m["unsafe_rejections_not_in_review"] == 1


def test_assess_run_adds_assessments_and_metrics_without_changing_any_verdict():
    ds = load_dataset(DS)
    plain = run_evaluation(ds, StrategyRef(config_dir=str(ROOT / "config")))
    shadow = run_evaluation(ds, StrategyRef(config_dir=str(ROOT / "config")), assess=True)
    strip = lambda r: [d.model_copy(update={"assessment": None}).model_dump() for d in r.decisions]   # noqa: E731
    assert strip(plain) == strip(shadow)
    assert all(d.assessment and d.assessment["state"] in ("accept", "review", "reject") for d in shadow.decisions)
    assert all(d.assessment["calibrated"] is False for d in shadow.decisions)
    assert "review" not in plain.metrics and shadow.metrics["review"]["status"] == "insufficient_evidence"
    # rejections made on text/rule-backed evidence stand; accepted jobs are accept/review, never reject
    for d in shadow.decisions:
        assert d.assessment["state"] != ("reject" if d.verdict == "accept" else "accept")


def test_golden_is_untouched_by_the_assessment_field():
    shadow = run_evaluation(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config")), assess=False)
    assert to_golden(shadow) == read_golden(GOLDEN)


def test_cli_assess_prints_the_review_section(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    assert cli_main(["run", str(DS), "--no-write", "--markdown", "--assess"]) == 0
    out = capsys.readouterr().out
    assert "Review (disagreement engine, shadow" in out and "insufficient_evidence" in out
