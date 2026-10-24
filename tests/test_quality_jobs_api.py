"""Quality page API (plan F9.1): background self-check / evaluation jobs, history, health summary."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.quality_jobs import (
    QualityJobContext, QualityJobError, QualityJobManager, QualityJobStore, _written_path,
    summarise_evaluation_run, summarise_feedback, summarise_self_check,
)

FAKE_SUMMARY = {"kind": "self_check", "status": "pass", "datasets": [], "feedback": {"status": "not_requested"}}


@pytest.fixture
def env(tmp_path: Path):
    from src.api.app import app

    names = ("db_path", "benchmark_runs_dir", "self_check_dir", "quality_jobs")
    saved = {n: getattr(app.state, n, None) for n in names}
    app.state.db_path = str(tmp_path / "jobs.db")
    app.state.benchmark_runs_dir = str(tmp_path / "runs")
    app.state.self_check_dir = str(tmp_path / "self-check")

    def use(executors: dict[str, Any] | None = None) -> QualityJobManager:
        app.state.quality_jobs = QualityJobManager(executors)
        return app.state.quality_jobs

    use({"self_check": lambda ctx: FAKE_SUMMARY, "evaluation": lambda ctx: FAKE_SUMMARY})
    yield TestClient(app), tmp_path, use
    app.state.quality_jobs.wait(10)
    for n, v in saved.items():
        setattr(app.state, n, v)


def _start(c: TestClient, kind: str = "self_check", **options: Any):
    return c.post("/api/quality/jobs", json={"kind": kind, "options": options})


def test_empty_history_and_health_are_well_formed(env):
    c, _, _ = env
    assert c.get("/api/quality/jobs").json() == {"active_id": None, "jobs": []}
    assert c.get("/api/quality/health").json() == {"self_check": None, "evaluation": None}
    assert c.get("/api/quality/jobs/nope").status_code == 404


def test_a_job_runs_in_the_background_and_lands_in_the_history(env):
    c, _, use = env
    seen: dict[str, Any] = {}

    def fake(ctx: QualityJobContext) -> dict[str, Any]:
        seen.update(options=ctx.options, kind=ctx.kind)
        ctx.set_phase("running")
        return FAKE_SUMMARY

    manager = use({"self_check": fake, "evaluation": fake})
    r = _start(c, include_feedback=False)
    assert r.status_code == 202
    job_id = r.json()["id"]
    manager.wait(10)
    job = c.get(f"/api/quality/jobs/{job_id}").json()
    assert job["status"] == "completed" and job["phase"] is None and job["summary"] == FAKE_SUMMARY
    assert job["error_code"] is None and job["finished_at"]
    assert seen == {"options": {"include_feedback": False}, "kind": "self_check"}
    history = c.get("/api/quality/jobs").json()
    assert history["active_id"] is None and [j["id"] for j in history["jobs"]] == [job_id]


def test_only_one_job_runs_at_a_time(env):
    c, _, use = env
    release = threading.Event()

    def slow(ctx: QualityJobContext) -> dict[str, Any]:
        release.wait(10)
        return FAKE_SUMMARY

    manager = use({"self_check": slow, "evaluation": slow})
    first = _start(c).json()["id"]
    busy = _start(c, "evaluation")
    assert busy.status_code == 409 and busy.json()["detail"] == {"code": "busy", "active_id": first}
    running = c.get("/api/quality/jobs").json()
    assert running["active_id"] == first and running["jobs"][0]["status"] == "running"
    release.set()
    manager.wait(10)
    assert _start(c, "evaluation").status_code == 202


def test_a_running_job_can_be_cancelled(env):
    c, _, use = env
    started = threading.Event()

    def waits_for_cancel(ctx: QualityJobContext) -> dict[str, Any]:
        started.set()
        ctx.cancel_event.wait(10)
        ctx.check_cancelled()
        return FAKE_SUMMARY

    manager = use({"self_check": waits_for_cancel, "evaluation": waits_for_cancel})
    job_id = _start(c).json()["id"]
    assert started.wait(10)
    assert c.post(f"/api/quality/jobs/{job_id}/cancel").status_code == 202
    manager.wait(10)
    assert c.get(f"/api/quality/jobs/{job_id}").json()["status"] == "cancelled"
    again = c.post(f"/api/quality/jobs/{job_id}/cancel")
    assert again.status_code == 409 and again.json()["detail"] == {"code": "not_running", "status": "cancelled"}
    assert c.post("/api/quality/jobs/unknown/cancel").status_code == 404


@pytest.mark.parametrize("error, code", [(QualityJobError("tool_failed"), "tool_failed"),
                                         (RuntimeError("boom"), "unexpected")])
def test_a_failure_is_recorded_as_a_code(env, error, code):
    c, _, use = env

    def fails(ctx: QualityJobContext) -> dict[str, Any]:
        raise error

    manager = use({"self_check": fails, "evaluation": fails})
    job_id = _start(c).json()["id"]
    manager.wait(10)
    job = c.get(f"/api/quality/jobs/{job_id}").json()
    assert job["status"] == "failed" and job["error_code"] == code and job["summary"] is None
    assert "boom" not in str(job)


def test_a_job_left_running_by_a_dead_process_is_reported_interrupted(env):
    c, tmp, _ = env
    QualityJobStore(tmp / "jobs.db").create("old", "self_check", {})
    job = c.get("/api/quality/jobs/old").json()
    assert job["status"] == "failed" and job["error_code"] == "interrupted"


@pytest.mark.parametrize("body", [{"kind": "promote"}, {"kind": "self_check", "options": {"dataset": "x"}},
                                  {"kind": "self_check", "extra": 1}, {}])
def test_invalid_requests_are_refused(env, body):
    c, _, _ = env
    assert c.post("/api/quality/jobs", json=body).status_code == 422


def test_a_foreign_origin_cannot_start_a_job(env):
    c, _, _ = env
    r = c.post("/api/quality/jobs", json={"kind": "self_check"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.get("/api/quality/jobs").json()["jobs"] == []


def test_the_report_path_must_be_inside_the_reports_folder(tmp_path):
    inside = tmp_path / "reports"
    inside.mkdir()
    (inside / "r.json").write_text("{}", encoding="utf-8")
    (tmp_path / "elsewhere.json").write_text("{}", encoding="utf-8")
    assert _written_path(f"noise\nwritten: {inside / 'r.json'}\n", inside) == (inside / "r.json").resolve()
    for stderr in (f"written: {tmp_path / 'elsewhere.json'}", "error: dataset missing", ""):
        with pytest.raises(QualityJobError, match="no_report"):
            _written_path(stderr, inside)


def test_summaries_are_codes_and_numbers():
    report = {"status": "safety_violation", "created_at": "2026-10-24T10:00:00+00:00", "datasets": [{
        "status": "safety_violation",
        "dataset": {"id": "real-v1", "version": "2", "kind": "real", "n_jobs": 120, "accept_labels": 12,
                    "label_provenance": {"human": 100, "assistant": 20}},
        "safety": {"unsafe_rejections": [{"job_id": "a", "reasons": ["r"]}], "rejections_without_reason": []},
        "eligibility": {"fp": 4, "precision": 0.75, "recall": 0.9},
        "uncertainty": {"review_usefulness": "insufficient_evidence"},
        "ranking": {"status": "not_measured", "why": "free text"},
        "baseline": {"status": "differs", "flips": {"fixed": ["x"], "broken": ["a"], "unjudged": []}},
        "limitations": ["free text the UI must not need"],
    }], "feedback": {"status": "measured", "jobs_with_explicit_feedback": 5, "decided_jobs": 50, "coverage": 0.1,
                     "contradictions": {"rejected_but_interested": {"count": 2}, "accepted_but_not_interested": {"count": 1}}}}
    s = summarise_self_check(report, report_file="r.json")
    d = s["datasets"][0]
    assert s["status"] == "safety_violation" and s["report_file"] == "r.json"
    assert d["unsafe_rejections"] == 1 and d["precision"] == 0.75 and d["recall"] == 0.9 and d["false_positives"] == 4
    assert d["ranking"] == "not_measured" and d["flips"] == {"fixed": 1, "broken": 1} and d["suitable"] == 12
    assert d["flags"] == ["few_suitable_labels", "labels_not_independent", "review_insufficient_evidence"]
    assert s["feedback"] == {"status": "measured", "answered": 5, "decided": 50, "coverage": 0.1, "contradictions": 3}
    assert "free text" not in str(s)
    assert summarise_feedback(None) == {"status": "not_requested"}
    assert summarise_feedback({"status": "unavailable", "why": "database not found: jobs.db"}) == {"status": "unavailable"}

    run = {"created_at": "2026-10-24T10:00:00+00:00", "warnings": ["w"],
           "dataset": {"id": "synthetic-ai-filter-50", "version": "1", "kind": "synthetic"},
           "metrics": {"n_jobs": 50, "overall": {"tp": 22, "fn": 0, "fp": 3, "precision": 0.88, "recall": 1.0},
                       "review": {"status": "insufficient_evidence"}, "label_provenance": {"basis": "independent"}}}
    e = summarise_evaluation_run(run)
    assert e["status"] == "measured" and e["datasets"][0]["unsafe_rejections"] == 0
    assert e["datasets"][0]["flags"] == ["synthetic_benchmark", "few_suitable_labels", "review_insufficient_evidence"]
    assert e["datasets"][0]["warnings"] == 1 and e["datasets"][0]["ranking"] == "not_measured"


# -- the real thing: the evaluation CLI in a child process, on the committed benchmark (a few seconds) ------

def test_the_real_self_check_and_evaluation_run_from_the_api(env):
    c, tmp, use = env
    manager = use()  # the default executors
    sc = _start(c).json()["id"]
    manager.wait(120)
    job = c.get(f"/api/quality/jobs/{sc}").json()
    assert job["status"] == "completed", job
    s = job["summary"]
    d = s["datasets"][0]
    assert s["status"] == "pass" and d["id"] == "synthetic-ai-filter-50" and d["unsafe_rejections"] == 0
    assert d["ranking"] == "not_measured" and "synthetic_benchmark" in d["flags"]
    # the temporary jobs.db carries no decision journal: coverage is reported unavailable, not invented
    assert s["feedback"] == {"status": "unavailable"}
    assert len(list((tmp / "self-check").glob("*.json"))) == 1

    ev = _start(c, "evaluation").json()["id"]
    manager.wait(120)
    job = c.get(f"/api/quality/jobs/{ev}").json()
    assert job["status"] == "completed", job
    d = job["summary"]["datasets"][0]
    assert d["unsafe_rejections"] == 0 and d["precision"] is not None and d["recall"] is not None

    health = c.get("/api/quality/health").json()
    assert health["self_check"]["status"] == "pass"
    assert health["evaluation"]["datasets"][0]["id"] == "synthetic-ai-filter-50"
    assert [j["kind"] for j in c.get("/api/quality/jobs").json()["jobs"]] == ["evaluation", "self_check"]


def test_cancelling_kills_the_child_process(env):
    c, _, use = env
    import sys

    def sleeper(ctx: QualityJobContext) -> dict[str, Any]:
        ctx.run_process([sys.executable, "-c", "import time; time.sleep(60)"])
        return FAKE_SUMMARY

    manager = use({"self_check": sleeper, "evaluation": sleeper})
    job_id = _start(c).json()["id"]
    threading.Event().wait(0.5)
    assert c.post(f"/api/quality/jobs/{job_id}/cancel").status_code == 202
    manager.wait(20)
    assert c.get(f"/api/quality/jobs/{job_id}").json()["status"] == "cancelled"
