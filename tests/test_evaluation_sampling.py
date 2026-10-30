"""Sampling cohorts: which numbers of an enriched real export describe the job stream, which do not."""

from __future__ import annotations

import json

import pytest

from src.evaluation.models import Decision, LabelRecord
from src.evaluation.sampling import load_sampling, sampling_metrics, wilson


def _label(jid: str, expected: str) -> LabelRecord:
    return LabelRecord(job_id=jid, expected=expected, label_source="human", labeler="t", labeled_at="2026-01-01")


def _dec(jid: str, verdict: str, score: float | None = 0.5) -> Decision:
    return Decision(job_id=jid, verdict=verdict, stage="passed" if verdict == "accept" else "filter",
                    score=score if verdict == "accept" else None)


STRATA = {"c1": "random_control", "c2": "random_control", "c3": "random_control", "c4": "random_control",
          "t1": "top_scored", "t2": "top_scored", "r1": "pipeline_rejected", "r2": "pipeline_rejected",
          "r3": "pipeline_rejected", "r4": "pipeline_rejected", "r5": "pipeline_rejected"}
LABELS = {j: _label(j, e) for j, e in [("c1", "accept"), ("c2", "reject"), ("c3", "reject"), ("c4", "borderline"),
                                       ("t1", "accept"), ("t2", "accept"), ("r1", "accept"), ("r2", "reject"),
                                       ("r3", "reject"), ("r4", "reject"), ("r5", "borderline")]}
DECISIONS = [_dec("c1", "accept", 0.7), _dec("c2", "accept", 0.6), _dec("c3", "reject"), _dec("c4", "accept"),
             _dec("t1", "accept", 0.9), _dec("t2", "accept", 0.8), *(_dec(f"r{i}", "reject") for i in range(1, 6))]


def test_the_representative_cohort_excludes_enriched_strata():
    m = sampling_metrics(DECISIONS, LABELS, STRATA, {"rejected_pool": 1000})
    rep = m["representative"]
    assert rep["stratum"] == "random_control" and rep["n_labelled"] == 4
    # c1 tp, c2 fp, c3 tn, c4 borderline: precision 1/2 on the uniform draw, not 3/4 as with t1/t2 pooled
    assert rep["overall"]["precision"] == pytest.approx(0.5)
    assert m["enriched_strata"] == ["top_scored"] and "over-represent" in m["warning"]
    assert m["by_stratum"]["top_scored"] == {"n": 2, "accept": 2, "reject": 0, "borderline": 0, "unlabelled": 0}
    assert m["by_stratum"]["pipeline_rejected"]["borderline"] == 1


def test_missed_opportunities_are_estimated_from_the_uniform_rejected_draw():
    """The pool holds every rejected job, borderline ones too: the rate's denominator must as well."""
    rej = sampling_metrics(DECISIONS, LABELS, STRATA, {"rejected_pool": 1000})["rejected_sample"]
    assert rej["n_labelled"] == 5 and rej["accept"] == 1 and rej["borderline"] == 1
    assert rej["accept_rate"] == pytest.approx(0.2)
    lo, hi = rej["accept_rate_ci95"]
    assert lo < 0.2 < hi and rej["pool"] == 1000
    assert rej["estimated_missed_accepts"] == [round(lo * 1000), 200, round(hi * 1000)]


def test_without_a_rejected_draw_nothing_is_estimated():
    strata = {k: v for k, v in STRATA.items() if v != "pipeline_rejected"}
    m = sampling_metrics(DECISIONS, LABELS, strata, {})
    assert m["rejected_sample"] is None and m["enriched_strata"] == ["top_scored"]


def test_wilson_interval_is_bounded_and_defined_at_the_edges():
    assert wilson(0, 0) is None
    lo, hi = wilson(0, 10)
    assert lo == 0 and 0 < hi < 0.35
    lo, hi = wilson(10, 10)
    assert 0.65 < lo < 1 and hi == pytest.approx(1)


def test_only_strata_and_pool_sizes_are_read_from_the_private_sidecar(tmp_path):
    (tmp_path / "PRIVATE_export_meta.json").write_text(json.dumps({
        "strata": {"j-1": "random_control"}, "rejected_pool": 7, "id_map": {"j-1": "real-id"}, "salt": "s"}))
    assert load_sampling(tmp_path) == ({"j-1": "random_control"}, {"rejected_pool": 7})
    assert load_sampling(tmp_path / "missing") is None


def test_run_with_sampling_reports_the_cohorts_end_to_end(tmp_path):
    """export (enriched + rejected draw) -> blind labels -> run --sampling: aggregates only."""
    import sqlite3

    from src.decisions.storage import DecisionStorage
    from src.evaluation.dataset import load_dataset
    from src.evaluation.export import export_real_dataset, import_annotations
    from src.evaluation.models import StrategyRef
    from src.evaluation.report import render_markdown
    from src.evaluation.runner import run_evaluation
    from tests.test_evaluation_export import _build_db, _write_annotations
    from tests.test_evaluation_export_rejected import _reject
    from pathlib import Path

    root_config = str(Path(__file__).resolve().parents[1] / "config")

    db = _build_db(tmp_path / "jobs.db")
    store = DecisionStorage(str(db))
    store.init_db()
    store.close()
    with sqlite3.connect(db) as conn:
        for i in range(6):
            _reject(conn, f"rj{i}", "filter", at="2026-10-01T00:00:00+00:00")
    out = tmp_path / "ds"
    export_real_dataset(db, out, n=8, seed=1, rejected=4, salt="salt-7c1e9b", as_of="2026-10-02T00:00:00+00:00")
    ids = [j["id"] for j in load_dataset(out, require_complete_labels=False).job_dicts]
    import_annotations(out, _write_annotations(out, {j: ("accept" if k % 3 == 0 else "reject")
                                                     for k, j in enumerate(ids)}), labeler="t")
    ds = load_dataset(out, require_complete_labels=False)
    res = run_evaluation(ds, StrategyRef(config_dir=root_config), sampling=True)
    sm = res.metrics["sampling"]
    assert sm["by_stratum"]["pipeline_rejected"]["n"] == 4 and sm["rejected_sample"]["pool"] == 6
    assert sm["representative"]["stratum"] == "random_control"
    dumped = json.dumps(res.model_dump())
    meta = json.loads((out / "PRIVATE_export_meta.json").read_text(encoding="utf-8"))
    assert not any(real in dumped for real in meta["id_map"].values()) and meta["salt"] not in dumped
    assert "## Sampling cohorts" in render_markdown(res)
    assert "sampling" not in run_evaluation(ds, StrategyRef(config_dir=root_config)).metrics   # opt-in
