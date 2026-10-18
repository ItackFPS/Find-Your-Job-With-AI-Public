"""Read-only lab/evaluation API: runs, experiments, memory and the active-strategy pointer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.lab.store import LabStore


@pytest.fixture
def client(tmp_path: Path):
    from src.api.app import app

    saved = (app.state.db_path, getattr(app.state, "lab_db_path", None), getattr(app.state, "benchmark_runs_dir", None),
             getattr(app.state, "self_check_dir", None))
    app.state.db_path = str(tmp_path / "jobs.db")
    app.state.lab_db_path = str(tmp_path / "lab.db")
    app.state.benchmark_runs_dir = str(tmp_path / "runs")
    app.state.self_check_dir = str(tmp_path / "self-check")
    yield TestClient(app), tmp_path
    (app.state.db_path, app.state.lab_db_path, app.state.benchmark_runs_dir, app.state.self_check_dir) = saved


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_everything_is_empty_but_well_formed_without_any_file(client):
    c, _ = client
    assert c.get("/api/lab/runs").json() == {"total": 0, "unreadable": 0, "runs": []}
    assert c.get("/api/lab/experiments").json() == {"experiments": []}
    assert c.get("/api/lab/memory").json() == {"items": []}
    assert c.get("/api/lab/self-check").json() == {"latest": None, "history": [], "unreadable": 0}
    body = c.get("/api/lab/active-strategy").json()
    assert body["current"] is None and body["honoured_by_pipeline"] is True
    assert body["pipeline"]["status"] == "default"


def test_runs_are_summarised_newest_first_and_bad_files_counted(client):
    c, tmp = client
    runs = tmp / "runs" / "real-v1"
    runs.mkdir(parents=True)
    for i, at in enumerate(("2026-10-01T00:00:00+00:00", "2026-10-03T00:00:00+00:00")):
        (runs / f"r{i}.json").write_text(json.dumps({
            "run_id": f"r{i}", "created_at": at, "warnings": ["w"],
            "dataset": {"id": "real-v1", "version": "2", "kind": "real", "jobs_sha256": "j", "labels_sha256": "l"},
            "strategy": {"name": "current", "fingerprint": "f" * 64},
            "metrics": {"n_labelled": 250, "overall": {"tp": 7, "fn": 3, "unsafe_rejection_rate": 0.3}, "review": None},
        }), encoding="utf-8")
    (runs / "broken.json").write_text("{", encoding="utf-8")
    body = c.get("/api/lab/runs").json()
    assert body["total"] == 2 and body["unreadable"] == 1
    assert [r["run_id"] for r in body["runs"]] == ["r1", "r0"]
    assert body["runs"][0]["overall"]["unsafe_rejection_rate"] == 0.3 and body["runs"][0]["file"] == "real-v1/r1.json"


def test_experiments_and_memory_are_read_without_writing_lab_db(client):
    c, tmp = client
    store = LabStore(tmp / "lab.db")
    hid = store.add_hypothesis("drop 'junior' from senior exclusions")
    cid = store.add_candidate(hid, "a" * 64, "/b", {"filters.yaml": {"x": 1}})
    store.add_experiment(cid, {"dataset_id": "d", "jobs_sha256": "j", "labels_sha256": "l"}, {
        "baseline_fingerprint": "b", "candidate_fingerprint": "c", "verdict": "improvement", "eligible_for_approval": True,
        "explanation": "CI95 [+0.1, +0.3]", "fixed": 13, "broken": 0,
        "guardrails": {"no_new_unsafe_rejection": {"passed": True}, "latency": {"passed": True}},
        "comparison": {"delta": {"fn": -13}, "limitations": ["only 22 accept label(s)"]}})
    store._conn.executescript("""
        CREATE TABLE memory_items (item_id TEXT, created_at TEXT, kind TEXT, statement TEXT, dataset_id TEXT,
                                   strategy_fingerprint TEXT, ref TEXT);
        CREATE TABLE memory_status (item_id TEXT, at TEXT, status TEXT);
        INSERT INTO memory_items VALUES ('m1', '2026-10-01', 'hypothesis', 'drop junior', 'd', 'f', NULL);
        INSERT INTO memory_status VALUES ('m1', '2026-10-01T00', 'active'), ('m1', '2026-10-02T00', 'confirmed');""")
    store.close()
    before = _sha(tmp / "lab.db")
    [e] = c.get("/api/lab/experiments").json()["experiments"]
    assert e["hypothesis"].startswith("drop 'junior'") and e["verdict"] == "improvement" and e["eligible_for_approval"]
    assert e["guardrails"] == {"no_new_unsafe_rejection": True, "latency": True} and e["delta"] == {"fn": -13}
    assert e["next_action"] == "submit_for_human_approval"
    # recorded before the evidence tier existed: eligible, but never presented as independently validated
    assert e["promotion_evidence"] == "exploratory_only" and e["independently_validated"] is False
    [m] = c.get("/api/lab/memory", params={"kind": "hypothesis"}).json()["items"]
    assert m["status"] == "confirmed"
    assert _sha(tmp / "lab.db") == before


def test_the_isolated_layout_lab_db_is_found_when_the_default_is_absent(tmp_path):
    from types import SimpleNamespace

    from src.api.routes.lab import _lab_db

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_path=str(tmp_path / "jobs.db"))))
    assert _lab_db(req) == tmp_path / "lab.db"                         # nothing yet: the default
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "lab.db").touch()                              # ADR 0011 isolated agent: data/lab/lab.db
    assert _lab_db(req) == tmp_path / "lab" / "lab.db"
    (tmp_path / "lab.db").touch()                                      # the default wins when both exist
    assert _lab_db(req) == tmp_path / "lab.db"


def test_active_strategy_history_is_read_from_jobs_db(client):
    c, tmp = client
    with sqlite3.connect(tmp / "jobs.db") as conn:
        conn.execute("CREATE TABLE active_strategy (pointer_id TEXT, set_at TEXT, bundle_hash TEXT, action TEXT)")
        conn.execute("INSERT INTO active_strategy VALUES ('p1', '2026-10-01', 'h1', 'approve'), "
                     "('p2', '2026-10-02', 'h0', 'rollback')")
    body = c.get("/api/lab/active-strategy").json()
    assert body["current"]["pointer_id"] == "p2" and len(body["history"]) == 2
    # recorded before the pipeline honoured pointers (no pipeline_effective column): never silently effective
    assert body["pipeline"]["status"] == "inert" and "config/" in body["note"]



def test_next_action_is_a_recommendation_per_verdict():
    from src.api.routes.lab import _next_action

    assert _next_action("regression", False, "proposed") == "reject"
    assert _next_action("inconclusive", False, None) == "investigate"
    assert _next_action("insufficient_evidence", False, None) == "investigate"
    assert _next_action("improvement", False, None) == "investigate"     # a guardrail failed: not eligible
    assert _next_action("improvement", True, "approved") == "approved"


def test_self_check_serves_the_latest_report_and_the_history(client):
    c, tmp = client
    d = tmp / "self-check"
    d.mkdir()
    for name, at, status in (("a.json", "2026-10-01T00:00:00+00:00", "pass"),
                             ("b.json", "2026-10-02T00:00:00+00:00", "regression")):
        (d / name).write_text(json.dumps({"created_at": at, "status": status,
                                          "datasets": [{"dataset": {"id": "synthetic"}, "status": status}]}),
                              encoding="utf-8")
    (d / "broken.json").write_text("{", encoding="utf-8")
    body = c.get("/api/lab/self-check").json()
    assert body["latest"]["status"] == "regression" and body["unreadable"] == 1
    assert [h["status"] for h in body["history"]] == ["regression", "pass"]
    assert body["history"][0]["datasets"] == {"synthetic": "regression"}
