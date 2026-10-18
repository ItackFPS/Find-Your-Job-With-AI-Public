"""End-to-end: the frozen-clock boundary through the evaluation runner, and the report sections."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.evaluation.dataset import load_dataset, write_dataset
from src.evaluation.metrics import FRESHNESS_REASON
from src.evaluation.models import LabelRecord, StrategyRef
from src.evaluation.report import render_markdown
from src.evaluation.runner import run_evaluation

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


def _age_dataset(tmp_path):
    base_ds = load_dataset(DS)
    base = next(j for j in base_ds.job_dicts if j["id"] == "bm01")
    as_of = base_ds.manifest.as_of
    ref = datetime.fromisoformat(as_of)
    ages = {
        "fresh": timedelta(days=2),
        "at_cutoff": timedelta(days=30),
        "past_cutoff": timedelta(days=30, seconds=1),
        "stale": timedelta(days=400),
    }
    jobs = [{**base, "id": k, "published_at": (ref - v).isoformat()} for k, v in ages.items()]
    labels = [LabelRecord(job_id=k, expected="accept", label_source="human", labeler="t", labeled_at="x") for k in ages]
    write_dataset(tmp_path, dataset_id="age", version="1", kind="synthetic", created_at=as_of, as_of=as_of,
                  jobs=jobs, labels=labels, profile_yaml=(DS / "profile.yaml").read_bytes())
    return load_dataset(tmp_path)


def _run(tmp_path):
    return run_evaluation(_age_dataset(tmp_path), StrategyRef(config_dir=str(ROOT / "config")))


def test_the_cutoff_boundary_holds_through_the_runner_and_shows_in_the_new_sections(tmp_path):
    res = _run(tmp_path)
    by = {d.job_id: d for d in res.decisions}
    assert by["fresh"].verdict == "accept" and by["at_cutoff"].verdict == "accept"
    assert by["past_cutoff"].reasons == [FRESHNESS_REASON] and by["stale"].reasons == [FRESHNESS_REASON]

    m = res.metrics
    assert m["rejections_by_reason"] == {FRESHNESS_REASON: {"total": 2, "by_label": {"accept": 2}}}
    assert (m["overall"]["tp"], m["overall"]["fn"]) == (2, 2)                   # default view: two 'unsafe rejections'
    diag = m["diagnostic_excluding_freshness"]
    assert diag["n_excluded"] == 2 and (diag["overall"]["tp"], diag["overall"]["fn"]) == (2, 0)


def test_the_markdown_report_labels_the_breakdown_and_warns_about_the_diagnostic(tmp_path):
    md = render_markdown(_run(tmp_path))
    assert "## Rejections by reason" in md and "| filter:published_age | 2 |" in md
    assert "NOT comparable with the overall metrics" in md and "different evaluation population" in md
    assert md.index("## Accept-class metrics") < md.index("## Rejections by reason") < md.index("## Diagnostic view")


def test_a_run_result_written_before_these_sections_existed_still_renders(tmp_path):
    res = _run(tmp_path)
    for key in ("rejections_by_reason", "diagnostic_excluding_freshness"):
        res.metrics.pop(key)
    md = render_markdown(res)
    assert "Rejections by reason" not in md and "Diagnostic view" not in md and "## By category" in md
