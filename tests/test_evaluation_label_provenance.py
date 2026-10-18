"""Metrics keep assistant-assisted labels apart from independent human labels."""

from __future__ import annotations

from src.evaluation.metrics import evaluate, label_provenance
from src.evaluation.models import Decision, LabelRecord


def _lab(jid: str, expected: str, source: str) -> LabelRecord:
    return LabelRecord(job_id=jid, expected=expected, label_source=source, labeler="t",  # type: ignore[arg-type]
                       labeled_at="2026-10-01")


def _dec(jid: str, verdict: str) -> Decision:
    return Decision(job_id=jid, verdict=verdict, stage="passed" if verdict == "accept" else "filter",  # type: ignore[arg-type]
                    reasons=[] if verdict == "accept" else ["filter:location"])


def test_only_independent_labels_enter_the_independent_diagnostic():
    labels = {"a": _lab("a", "accept", "human"), "b": _lab("b", "accept", "human_assisted"),
              "c": _lab("c", "reject", "human_assisted"), "d": _lab("d", "reject", "human")}
    decisions = [_dec("a", "reject"), _dec("b", "accept"), _dec("c", "reject"), _dec("d", "reject")]
    p = label_provenance(decisions, labels)
    assert p["by_source"] == {"human": {"accept": 1, "reject": 1}, "human_assisted": {"accept": 1, "reject": 1}}
    assert (p["n_independent"], p["n_assisted"]) == (2, 2)
    ind = p["independent_only"]
    assert ind["kind"] == "diagnostic_not_comparable" and ind["n_labelled"] == 2
    assert (ind["overall"]["fn"], ind["overall"]["tn"], ind["overall"]["tp"]) == (1, 1, 0)


def test_no_diagnostic_when_every_label_has_the_same_provenance():
    labels = {"a": _lab("a", "accept", "human")}
    p = evaluate([_dec("a", "accept")], labels)["label_provenance"]
    assert p == {"by_source": {"human": {"accept": 1}}, "n_independent": 1, "n_assisted": 0, "n_derived": 0,
                 "n_recorded_human_but_assisted": 0, "basis": "independent",
                 "by_class": {"accept": {"total": 1, "independent": 1}}}


def test_report_renders_the_provenance_section():
    from pathlib import Path

    from src.evaluation.dataset import load_dataset
    from src.evaluation.models import StrategyRef
    from src.evaluation.report import render_markdown
    from src.evaluation.runner import run_evaluation

    root = Path(__file__).resolve().parents[1]
    ds = load_dataset(root / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50")
    out = render_markdown(run_evaluation(ds, StrategyRef(
        profile_path=str(root / "tests" / "fixtures" / "ai_filter_benchmark" / "profile.yaml"))))
    assert "## Label provenance" in out and "`human` (independent)" in out


def test_basis_and_per_class_independence():
    labels = {"a": _lab("a", "accept", "human_assisted"), "b": _lab("b", "reject", "human"),
              "c": _lab("c", "reject", "derived")}
    p = label_provenance([], labels)
    assert p["basis"] == "mixed" and p["n_derived"] == 1
    assert p["by_class"] == {"accept": {"total": 1, "independent": 0}, "reject": {"total": 2, "independent": 1}}
    assert label_provenance([], {"a": labels["a"]})["basis"] == "not_independent"
    assert label_provenance([], {})["basis"] == "none"


def test_a_human_label_over_a_recorded_proposal_is_not_counted_as_independent():
    labels = {"a": _lab("a", "accept", "human"), "b": _lab("b", "reject", "human")}
    p = label_provenance([_dec("a", "accept"), _dec("b", "reject")], labels, assisted=frozenset({"a"}))
    assert (p["n_independent"], p["n_assisted"], p["n_recorded_human_but_assisted"]) == (1, 1, 1)
    assert p["basis"] == "mixed" and p["by_class"]["accept"] == {"total": 1, "independent": 0}
    assert p["independent_only"]["n_labelled"] == 1          # only b


def _result(labels: dict, decisions: list, warnings: list | None = None):
    from src.evaluation.metrics import bootstrap_ci
    from src.evaluation.models import RunResult

    m = evaluate(decisions, labels)
    m["bootstrap_ci_95"] = bootstrap_ci([(labels[d.job_id].expected, d.verdict) for d in decisions])
    return RunResult(
        run_id="r" * 8, created_at="2026-10-14T00:00:00+00:00",
        dataset={"id": "d", "version": "1", "kind": "real", "jobs_sha256": "x", "labels_sha256": "y", "as_of": "z"},
        strategy={"name": "s", "fingerprint": "f" * 12, "files": {}}, code={}, environment={},
        llm={"enabled": False, "calls": 0}, decisions=decisions, metrics=m,
        timings={"stage_seconds": {}, "per_job_ms_mean": 0.0}, warnings=warnings or [])


def test_the_report_states_the_evidence_before_the_metrics():
    from src.evaluation.report import render_markdown

    labels = {"a": _lab("a", "accept", "human_assisted"), "b": _lab("b", "reject", "human_assisted")}
    out = render_markdown(_result(labels, [_dec("a", "reject"), _dec("b", "reject")],
                                  ["real dataset: only jobs accepted at export time (survival bias); x"]))
    banner = out.index("**NOT independent validation**")
    assert banner < out.index("## Accept-class metrics")
    assert "accept labels 1 (0 independent)" in out and "label basis: **not_independent**" in out
    assert "sampling: real dataset: only jobs accepted" in out and "Only 1 accept label(s)" in out
    indep = {"a": _lab("a", "accept", "human"), "b": _lab("b", "reject", "human")}
    assert "NOT independent validation" not in render_markdown(_result(indep, [_dec("a", "accept"), _dec("b", "reject")]))
