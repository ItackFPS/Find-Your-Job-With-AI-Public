"""Quality › the owner's own labels (P2.5/P2.6): held-out ranking with CIs, missed offers, FP breakdown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.quality_jobs import QualityJobManager, QualityJobStore, summarise_personal_run
from src.evaluation.dataset import load_dataset, write_dataset

ROOT = Path(__file__).resolve().parents[1]
SYNTH = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


@pytest.fixture()
def data(tmp_path):
    """A small 'real' set in <data>/calibration/, built from the synthetic jobs and their labels."""
    ds = load_dataset(SYNTH)
    dest = tmp_path / "calibration" / "real-t"
    write_dataset(dest, dataset_id="real-t", version="2", kind="real", created_at=ds.manifest.created_at,
                  as_of=ds.manifest.as_of, jobs=ds.job_dicts, labels=list(ds.labels.values()),
                  profile_yaml=ds.profile_path.read_bytes())
    strata = {j["id"]: ("random_control" if i % 2 else "top_scored") for i, j in enumerate(ds.job_dicts)}
    (dest / "PRIVATE_export_meta.json").write_text(json.dumps({"strata": strata}), encoding="utf-8")
    return tmp_path


def _run(data: Path, **options):
    manager = QualityJobManager()
    job_id = manager.start("personal", options, db_path=data / "jobs.db", config_dir=ROOT / "config",
                           self_check_dir=data / "benchmarks" / "self-check", runs_dir=data / "benchmarks" / "runs")
    manager.wait(300)
    return QualityJobStore(data / "jobs.db").get(job_id)


def test_measures_the_held_out_part_of_an_imported_set(data):
    job = _run(data, dataset_id="real-t", split="heldout")
    assert job["status"] == "completed", job
    s = job["summary"]
    full = load_dataset(data / "calibration" / "real-t")
    assert s["kind"] == "personal" and s["dataset"]["split"] == "heldout"
    assert 0 < s["dataset"]["labels"] < len(full.labels)
    assert set(s["ranking"]) >= {"auc", "auc_ci95", "precision_at_strong", "recall_at_20"}
    assert "pooled_over_enriched_strata" in s["flags"]           # top_scored is an enriched stratum
    assert "few_suitable_labels" in s["flags"]
    assert isinstance(s["false_positives"]["by_source"], dict)
    assert not (data / "benchmarks" / "runs").exists()           # never mixed with the benchmark runs


def test_an_unknown_set_fails_with_a_code(data):
    job = _run(data, dataset_id="nope")
    assert (job["status"], job["error_code"]) == ("failed", "not_found")


def test_the_summary_keeps_freshness_only_misses_and_fp_breakdown_apart():
    run = {"dataset": {"id": "real-v2@heldout", "version": "3"}, "created_at": "2026-11-01T00:00:00+00:00",
           "metrics": {"split": {"name": "heldout", "labels": 40},
                       "overall": {"tp": 8, "fn": 3, "fp": 5},
                       "diagnostic_excluding_freshness": {"overall": {"fn": 1}},
                       "by_category": {"location": {"fp": 3}, "domain": {"fp": 2}, "skills": {"fp": 0}},
                       "false_positives_by_source": {"france-travail-web": 4, "eures": 1},
                       "ranking": {"auc": 0.7, "auc_ci95": [0.55, 0.82], "precision_at_strong": {"value": 0.5, "ci95": [0.2, 0.8], "n": 6},
                                   "recall_at_20": {"value": 0.6, "ci95": [0.3, 0.85], "in_top": 6, "n_accept": 10}},
                       "sampling": {"enriched_strata": [], "rejected_sample": {"n_labelled": 12, "accept": 1,
                                    "accept_rate_ci95": [0.01, 0.35], "estimated_missed_accepts": [10, 100, 600], "pool": 1200}}}}
    s = summarise_personal_run(run)
    assert (s["unsafe_rejections"], s["unsafe_freshness_only"]) == (3, 2)
    assert s["false_positives"] == {"total": 5, "by_category": {"domain": 2, "location": 3},
                                    "by_source": {"france-travail-web": 4, "eures": 1}}
    assert s["missed"]["estimate"] == [10, 100, 600] and s["ranking"]["auc_ci95"] == [0.55, 0.82]
    assert s["flags"] == ["few_suitable_labels"]


def test_the_api_requires_a_set_for_a_personal_measure(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import app

    saved = app.state.db_path
    app.state.db_path = str(tmp_path / "jobs.db")
    try:
        r = TestClient(app).post("/api/quality/jobs", json={"kind": "personal"})
        bad = TestClient(app).post("/api/quality/jobs", json={"kind": "personal", "options": {"dataset_id": "../x"}})
    finally:
        app.state.db_path = saved
    assert r.status_code == 400 and r.json()["detail"]["code"] == "dataset_required"
    assert bad.status_code == 422
