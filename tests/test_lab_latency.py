"""The lab's latency guardrail compares speed on comparable work and keeps the aggregate cost visible.

Timings here are fabricated (no wall clock), so every assertion is about the metric, not the machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.evaluation.dataset import load_dataset
from src.evaluation.models import RunResult, StrategyRef
from src.evaluation.runner import run_evaluation
from src.lab.bundle import create_bundle
from src.lab.experiment import run_experiment
from src.lab.latency import latency_guardrail

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
PROFILE = ROOT / "tests" / "fixtures" / "ai_filter_benchmark" / "profile.yaml"


@pytest.fixture(scope="module")
def real_run():
    return run_evaluation(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(PROFILE)))


def _run(template: RunResult, *, jobs: dict[str, int], ms_per_job: dict[str, float],
         extra_ms: dict[str, float] | None = None) -> RunResult:
    """A replay whose stage ``s`` processed ``jobs[s]`` jobs at ``ms_per_job[s]`` each (+ ``extra_ms``)."""
    seconds = {s: (jobs[s] * ms_per_job[s] + (extra_ms or {}).get(s, 0.0)) / 1000 for s in jobs}
    total = sum(seconds.values())
    return template.model_copy(update={"timings": {
        "stage_seconds": {**seconds, "total": total}, "stage_jobs": dict(jobs),
        "per_job_ms_mean": total * 1000 / jobs["filter"]}})


COST = {"filter": 0.6, "requirements_gate": 1.4, "scoring": 2.0, "assessment_accepted": 5.0, "assessment_rejected": 1.0}


def _workload(accepted: int, passing: int, n: int = 50) -> dict[str, int]:
    return {"filter": n, "requirements_gate": passing, "scoring": accepted,
            "assessment_accepted": accepted, "assessment_rejected": n - accepted}


def test_accepting_more_jobs_at_the_same_speed_passes_and_shows_the_extra_cost(real_run):
    base = _run(real_run, jobs=_workload(accepted=12, passing=13), ms_per_job=COST)
    cand = _run(real_run, jobs=_workload(accepted=25, passing=30), ms_per_job=COST)
    g = latency_guardrail([base, base], [cand, cand])
    assert g["passed"] and g["workload_adjusted_ratio"] == pytest.approx(1.0)
    # the aggregate cost is not hidden: more work is reported, just not called slower
    assert g["aggregate_ratio"] > 1.5 and g["candidate_ms"] > g["baseline_ms"]
    assert g["stages"]["scoring"] == {"baseline_jobs": 12, "candidate_jobs": 25, "baseline_ms_per_job": 2.0,
                                      "candidate_ms_per_job": 2.0, "per_job_ratio": 1.0}
    # the former measure (total ÷ dataset size) would have failed this candidate
    assert cand.timings["per_job_ms_mean"] > base.timings["per_job_ms_mean"] * 1.5


def test_a_genuinely_slower_candidate_fails_whatever_its_workload(real_run):
    base = _run(real_run, jobs=_workload(accepted=12, passing=13), ms_per_job=COST)
    slower = {s: c * 2 for s, c in COST.items()}
    same_work = _run(real_run, jobs=_workload(accepted=12, passing=13), ms_per_job=slower)
    less_work = _run(real_run, jobs=_workload(accepted=5, passing=6), ms_per_job=slower)
    for cand in (same_work, less_work):
        g = latency_guardrail([base, base], [cand, cand])
        assert not g["passed"] and g["workload_adjusted_ratio"] == pytest.approx(2.0)
    # less work but twice as slow per job: the aggregate alone would have hidden it
    assert latency_guardrail([base], [less_work])["aggregate_ratio"] < 1.5


def test_a_slowdown_in_one_heavy_stage_fails(real_run):
    base = _run(real_run, jobs=_workload(accepted=25, passing=30), ms_per_job=COST)
    cand = _run(real_run, jobs=_workload(accepted=25, passing=30), ms_per_job={**COST, "requirements_gate": 7.0})
    g = latency_guardrail([base, base], [cand, cand])
    assert not g["passed"] and g["stages"]["requirements_gate"]["per_job_ratio"] == pytest.approx(5.0)


def test_cold_start_is_charged_to_neither_side(real_run):
    work = _workload(accepted=12, passing=13)
    warm = _run(real_run, jobs=work, ms_per_job=COST)
    cold = _run(real_run, jobs=work, ms_per_job=COST, extra_ms={"assessment_rejected": 15_000.0})
    twice_slower = _run(real_run, jobs=work, ms_per_job={s: c * 2 for s, c in COST.items()})
    # a cold baseline replay must not make a slower candidate look fast ...
    assert latency_guardrail([cold], [twice_slower, twice_slower])["passed"]          # one replay: fooled
    assert not latency_guardrail([cold, warm], [twice_slower, twice_slower])["passed"]
    # ... nor a cold candidate replay make an equal one look slow
    assert latency_guardrail([warm, warm], [cold, warm])["passed"]


def test_a_stage_the_baseline_never_reached_counts_at_par(real_run):
    base = _run(real_run, jobs=_workload(accepted=0, passing=0), ms_per_job=COST)
    cand = _run(real_run, jobs=_workload(accepted=10, passing=12), ms_per_job=COST)
    g = latency_guardrail([base], [cand])
    assert g["passed"] and set(g["unmeasured_in_baseline"]) == {"requirements_gate", "scoring", "assessment_accepted"}


def test_replays_with_different_workloads_fail(real_run):
    a = _run(real_run, jobs=_workload(accepted=12, passing=13), ms_per_job=COST)
    b = _run(real_run, jobs=_workload(accepted=13, passing=13), ms_per_job=COST)
    assert latency_guardrail([a, a], [a, b]) == {"passed": False, "method": "workload_adjusted_stages",
                                                 "why": "replays of one strategy processed different workloads"}


def test_results_without_stage_workload_use_the_former_measure(real_run):
    legacy = real_run.model_copy(update={"timings": {"stage_seconds": {}, "per_job_ms_mean": 1.0}})
    slow = real_run.model_copy(update={"timings": {"stage_seconds": {}, "per_job_ms_mean": 9.0}})
    assert latency_guardrail([legacy], [legacy])["method"] == "aggregate_per_job"
    assert not latency_guardrail([legacy], [slow])["passed"]


def test_replays_record_the_workload_of_every_stage(real_run):
    assessed = run_evaluation(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(PROFILE)),
                              assess=True)
    jobs = assessed.timings["stage_jobs"]
    accepted = sum(d.verdict == "accept" for d in assessed.decisions)
    assert jobs["filter"] == 50 and jobs["scoring"] == jobs["assessment_accepted"] == accepted
    assert jobs["assessment_rejected"] == 50 - accepted and accepted <= jobs["requirements_gate"] <= 50
    assert set(jobs) <= set(assessed.timings["stage_seconds"])
    assert "assessment_accepted" not in real_run.timings["stage_jobs"]                 # not assessed: not timed


def test_an_experiment_measures_each_side_on_two_interleaved_replays(tmp_path):
    _, bundle = create_bundle(ROOT / "config", {}, tmp_path)
    res = run_experiment(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(PROFILE)), bundle)
    lat = res["guardrails"]["latency"]
    assert lat["method"] == "workload_adjusted_stages" and lat["replays"] == {"baseline": 2, "candidate": 2}
    assert all(s["baseline_jobs"] == s["candidate_jobs"] for s in lat["stages"].values())    # identical config
