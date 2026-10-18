"""Runner determinism, equivalence with production wiring, golden, compare, CLI."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.evaluation.__main__ import main as cli_main
from src.evaluation.compare import compare_runs
from src.evaluation.dataset import load_dataset, write_dataset
from src.evaluation.models import LabelRecord, StrategyRef
from src.evaluation.runner import frozen_clock, run_evaluation
from src.evaluation.store import (
    diff_golden, read_golden, read_run_result, to_golden, write_run_result,
)

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
GOLDEN = ROOT / "tests" / "fixtures" / "evaluation" / "golden" / "synthetic-ai-filter-50.json"


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DS)


@pytest.fixture(scope="module")
def result(dataset):
    return run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))


def test_runs_are_deterministic(dataset, result):
    again = run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))
    assert [d.model_dump() for d in again.decisions] == [d.model_dump() for d in result.decisions]
    assert again.metrics["overall"] == result.metrics["overall"]
    assert again.strategy["fingerprint"] == result.strategy["fingerprint"]


def test_equivalent_to_existing_benchmark_script(result):
    """The runner calls the same production functions as the pre-existing script."""
    from scripts.benchmark_ai_filter_50 import run_benchmark

    old = run_benchmark(
        config_dir=str(ROOT / "config"), run_ai=False, run_phase2=False,
        profile_path=str(ROOT / "tests" / "fixtures" / "ai_filter_benchmark" / "profile.yaml"),
    )["det_verdicts"]
    new = {d.job_id: (d.verdict, d.stage) for d in result.decisions}
    assert {k: (v.verdict.lower(), v.stage) for k, v in old.items()} == new


def test_golden_decisions_unchanged(result):
    """Change detector (NOT a quality target): fails when any decision/reason changes.

    If it fails after an intentional change, review the diff then run
    `python -m src.evaluation golden <dataset> <golden> --update`.
    """
    diffs = diff_golden(read_golden(GOLDEN), to_golden(result))
    assert not diffs, "\n".join(diffs)


def test_labels_do_not_influence_decisions(dataset):
    flipped = {
        k: v.model_copy(update={"expected": "reject" if v.expected == "accept" else "accept"})
        for k, v in dataset.labels.items()
    }
    ds2 = type(dataset)(dataset.path, dataset.manifest, dataset.job_dicts, flipped, ())
    a = run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))
    b = run_evaluation(ds2, StrategyRef(config_dir=str(ROOT / "config")))
    assert [d.model_dump() for d in a.decisions] == [d.model_dump() for d in b.decisions]


def test_real_dataset_warns_about_survival_bias(tmp_path, dataset):
    jobs = [j for j in dataset.job_dicts][:3]
    labels = [LabelRecord(job_id=j["id"], expected="accept", label_source="human", labeler="t", labeled_at="x") for j in jobs]
    write_dataset(tmp_path, dataset_id="r", version="1", kind="real", created_at="2026-01-01",
                  as_of=dataset.manifest.as_of, jobs=jobs, labels=labels,
                  profile_yaml=(DS / "profile.yaml").read_bytes())
    res = run_evaluation(load_dataset(tmp_path), StrategyRef(config_dir=str(ROOT / "config")))
    assert any("survival bias" in w for w in res.warnings)


def test_frozen_clock_makes_the_age_filter_reproducible(tmp_path, dataset):
    base = next(j for j in dataset.job_dicts if j["id"] == "bm01")
    as_of = dataset.manifest.as_of
    from datetime import datetime
    ref = datetime.fromisoformat(as_of)
    jobs = [
        {**base, "id": "fresh", "published_at": (ref - timedelta(days=2)).isoformat()},
        {**base, "id": "stale", "published_at": (ref - timedelta(days=400)).isoformat()},
    ]
    labels = [LabelRecord(job_id=i, expected="accept", label_source="human", labeler="t", labeled_at="x") for i in ("fresh", "stale")]
    write_dataset(tmp_path, dataset_id="age", version="1", kind="synthetic", created_at=as_of, as_of=as_of,
                  jobs=jobs, labels=labels, profile_yaml=(DS / "profile.yaml").read_bytes())
    res = run_evaluation(load_dataset(tmp_path), StrategyRef(config_dir=str(ROOT / "config")))
    by = {d.job_id: d for d in res.decisions}
    assert by["fresh"].verdict == "accept"
    assert by["stale"].verdict == "reject" and by["stale"].reasons == ["filter:published_age"]


def test_frozen_clock_restores_datetime():
    import src.pipeline.filter as f
    from datetime import datetime
    original = f.datetime
    with frozen_clock(datetime.fromisoformat("2020-01-01T00:00:00+00:00")):
        assert f.datetime.now().year == 2020
    assert f.datetime is original


def test_run_result_is_written_exclusively_and_roundtrips(result, tmp_path):
    p = write_run_result(result, tmp_path)
    assert read_run_result(p).run_id == result.run_id
    with pytest.raises(FileExistsError):
        write_run_result(result, tmp_path)


def test_compare_identifies_flips_and_refuses_other_datasets(dataset, result):
    cand = result.model_copy(deep=True)
    victim = next(d for d in cand.decisions if d.verdict == "accept")
    victim.verdict = "reject"
    victim.stage = "filter"
    labels = {k: v.expected for k, v in dataset.labels.items()}
    out = compare_runs(result, cand, labels)
    assert out["n_verdict_flips"] == 1 and out["flips"][0]["job_id"] == victim.job_id
    expected_broken = 1 if labels[victim.job_id] == "accept" else 0
    assert out["broken"] == expected_broken
    other = result.model_copy(deep=True)
    other.dataset["jobs_sha256"] = "x"
    with pytest.raises(ValueError, match="different datasets"):
        compare_runs(result, other)


def test_compare_splits_flips_by_label_provenance_and_warns_on_dependent_labels(dataset, result):
    cand = result.model_copy(deep=True)
    flipped = [d for d in cand.decisions if d.verdict == "accept" and dataset.labels[d.job_id].expected != "borderline"][:2]
    for d in flipped:
        d.verdict, d.stage = "reject", "filter"
    labels = {k: v.expected for k, v in dataset.labels.items()}
    provenance = {k: "human" for k in labels} | {flipped[0].job_id: "human_assisted"}
    out = compare_runs(result, cand, labels, provenance)
    assert sum(v["fixed"] + v["broken"] for v in out["flips_by_provenance"].values()) == out["fixed"] + out["broken"] == 2
    assert set(out["flips_by_provenance"]) == {"human", "human_assisted"}
    assert out["label_provenance"]["human_assisted"] == 1 and "1 of 2" in out["warnings"][0]
    assert "warnings" not in compare_runs(result, cand, labels, {k: "human" for k in labels})


def test_llm_unreachable_is_an_explicit_error(dataset):
    from src.evaluation.models import LLMSpec
    from src.evaluation.runner import EvaluationError

    strat = StrategyRef(config_dir=str(ROOT / "config"), llm=LLMSpec(endpoint="http://127.0.0.1:9", model_name="x"))
    with pytest.raises(EvaluationError, match="unreachable"):
        run_evaluation(dataset, strat)


def test_cli_validate_run_golden(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert cli_main(["validate", str(DS)]) == 0
    assert cli_main(["run", str(DS), "--out", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert cli_main(["golden", str(DS), str(GOLDEN), "--check"]) == 0
    bad = tmp_path / "bad.json"
    g = json.loads(GOLDEN.read_text())
    g["decisions"][0]["verdict"] = "reject" if g["decisions"][0]["verdict"] == "accept" else "accept"
    bad.write_text(json.dumps(g))
    assert cli_main(["golden", str(DS), str(bad), "--check"]) == 1
    assert cli_main(["validate", str(tmp_path)]) == 2


def test_fingerprint_distinguishes_llm_from_deterministic():
    from src.evaluation.fingerprint import strategy_fingerprint

    base, _ = strategy_fingerprint({"f": ROOT / "config" / "filters.yaml"})
    llm, _ = strategy_fingerprint({"f": ROOT / "config" / "filters.yaml"}, extra={"model_name": "m"})
    assert base != llm


def test_compare_refuses_different_job_sets_and_as_of(result):
    truncated = result.model_copy(deep=True)
    truncated.decisions = truncated.decisions[1:]
    with pytest.raises(ValueError, match="different job ids"):
        compare_runs(result, truncated)
    other = result.model_copy(deep=True)
    other.dataset["as_of"] = "2030-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="as_of"):
        compare_runs(result, other)


def test_cli_compare_refuses_labels_other_than_the_runs_were_scored_on(dataset, result, tmp_path, capsys):
    from src.evaluation.dataset import write_dataset

    for name in ("a", "b"):
        write_run_result(result, tmp_path / name)
    a, b = (next((tmp_path / n).glob("*.json")) for n in ("a", "b"))
    assert cli_main(["compare", str(a), str(b), "--dataset", str(DS)]) == 0
    capsys.readouterr()
    relabelled = [lab.model_copy(update={"expected": "reject" if lab.expected == "accept" else lab.expected})
                  for lab in dataset.labels.values()]
    m = dataset.manifest
    write_dataset(tmp_path / "v2", dataset_id=m.dataset_id, version="2", kind=m.kind, created_at=m.created_at,
                  as_of=m.as_of, jobs=dataset.job_dicts, labels=relabelled)
    assert cli_main(["compare", str(a), str(b), "--dataset", str(tmp_path / "v2")]) == 2
    assert "labels_sha256" in capsys.readouterr().err


def test_golden_diff_names_each_changed_field(result):
    g = to_golden(result)
    mutated = g.model_copy(deep=True)
    mutated.decisions[0].flags = ["x"]
    mutated.decisions[0].extraction_method = "llm"
    out = diff_golden(g, mutated)
    assert len(out) == 1 and "flags" in out[0] and "extraction_method" in out[0]


def test_cli_missing_files_exit_2_not_1(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert cli_main(["golden", str(DS), str(tmp_path / "missing.json"), "--check"]) == 2
    assert cli_main(["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 2


def test_scores_present_exactly_for_accepted_jobs(result):
    for d in result.decisions:
        if d.verdict == "accept":
            assert d.score is not None and 0.0 <= d.score <= 1.0
            assert set(d.factors) == {"location_fit", "domain_relevance", "skills_match",
                                      "seniority_match", "experience_fit"}
        else:
            assert d.score is None and d.factors is None


def test_scoring_does_not_depend_on_a_local_esco_database(dataset, tmp_path):
    """Default strategy disables ESCO enrichment; pointing at a different DB changes the fingerprint."""
    a = run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))
    fake = tmp_path / "esco.db"
    fake.write_bytes(b"not a database")
    from src.evaluation.runner import EvaluationError
    with pytest.raises(EvaluationError, match="does not exist"):
        run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config"), esco_db=str(tmp_path / "nope.db")))
    b = run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config"), esco_db=str(fake)))
    assert "esco_db" not in a.strategy["files"] and "esco_db" in b.strategy["files"]
    assert a.strategy["fingerprint"] != b.strategy["fingerprint"]
    # a broken ESCO db degrades to "no enrichment" exactly like production does
    assert [d.score for d in a.decisions] == [d.score for d in b.decisions]


def test_scoring_yaml_is_part_of_the_fingerprint(dataset, tmp_path):
    import shutil

    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    base = run_evaluation(dataset, StrategyRef(config_dir=str(cfg)))
    (cfg / "scoring.yaml").write_text((cfg / "scoring.yaml").read_text() + "\n# touched\n")
    assert run_evaluation(dataset, StrategyRef(config_dir=str(cfg))).strategy["fingerprint"] != base.strategy["fingerprint"]


def test_compare_reports_the_paired_test_over_independent_labels_only(dataset, result):
    cand = result.model_copy(deep=True)
    flipped = [d for d in cand.decisions if d.verdict == "accept" and dataset.labels[d.job_id].expected == "accept"][:3]
    for d in flipped:
        d.verdict, d.stage = "reject", "filter"
    labels = {k: v.expected for k, v in dataset.labels.items()}
    provenance = {k: "human" for k in labels} | {d.job_id: "human_assisted" for d in flipped[:2]}
    out = compare_runs(result, cand, labels, provenance)
    assert out["broken"] == 3 and out["independent"]["broken"] == 1 and out["independent"]["fixed"] == 0
    assert out["independent"]["mcnemar_exact_p"] == 1.0
