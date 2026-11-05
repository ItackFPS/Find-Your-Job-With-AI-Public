"""Lanes for weak-evidence offers (ADR 0019 v5 AD-2b; stage L5.3)."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from src.pipeline import lanes as L

FLAG = [{"code": "published_age", "observed": None, "expected": None, "severity": 1.0}]


def _bd(domain=0.9, weak=True):
    return {"domain_relevance": domain, **({"weak_flags": FLAG} if weak else {})}


def test_unflagged_offers_stay_in_main():
    got = L.assign_lanes([("a", 0.2, _bd(weak=False)), ("b", 0.9, {}), ("c", 0.5, None)])
    assert got == {"a": L.MAIN, "b": L.MAIN, "c": L.MAIN}


def test_floors_route_weak_offers():
    got = L.assign_lanes([("ok", 0.70, _bd()), ("low", 0.59, _bd()), ("offdomain", 0.90, _bd(domain=0.69)),
                          ("nodomain", 0.90, {"weak_flags": FLAG})])
    assert got == {"ok": L.OUTSIDE, "low": L.BELOW, "offdomain": L.BELOW, "nodomain": L.BELOW}


def test_the_lane_never_exceeds_its_size():
    rng = random.Random(7)
    for trial in range(200):
        rows = [(f"j{trial}-{i}", rng.random(), _bd(domain=rng.random(), weak=rng.random() < 0.7))
                for i in range(rng.randint(0, 60))]
        size = rng.randint(0, 12)
        got = L.assign_lanes(rows, L.LaneParams(size=size))
        outside = [j for j, lane in got.items() if lane == L.OUTSIDE]
        assert len(outside) <= size
        assert set(got) == {r[0] for r in rows}                              # nothing dropped
        assert all(got[j] == L.MAIN for j, _s, b in rows if "weak_flags" not in b)


def test_the_lane_takes_the_best_first_deterministically():
    rows = [(f"j{i:02d}", 0.61 + i / 100, _bd()) for i in range(15)] + [("tie-b", 0.99, _bd()), ("tie-a", 0.99, _bd())]
    got = L.assign_lanes(rows, L.LaneParams(size=3))
    assert sorted(j for j, lane in got.items() if lane == L.OUTSIDE) == ["j14", "tie-a", "tie-b"]
    got2 = L.assign_lanes(list(reversed(rows)), L.LaneParams(size=2))
    assert sorted(j for j, lane in got2.items() if lane == L.OUTSIDE) == ["tie-a", "tie-b"]


def test_params_from_scoring_yaml(tmp_path):
    assert L.load_lane_params(Path("config/scoring.yaml")) == L.LaneParams(10, 0.60, 0.7)
    assert L.load_lane_params(tmp_path / "missing.yaml") == L.LaneParams()
    p = tmp_path / "s.yaml"
    p.write_text("scoring:\n  weak_evidence:\n    outside_lane_size: 3\n    outside_score_floor: 2\n"
                 "    outside_domain_floor: true\n", encoding="utf-8")
    assert L.load_lane_params(p) == L.LaneParams(3, 0.60, 0.7)


# -- pipeline and API ---------------------------------------------------------------------------

def test_pipeline_journals_lanes(tmp_path):
    from tests.test_decision_journal_orchestrator import _config_dir
    from tests.test_filter_weak_evidence_mode import _rank_all_filters
    from tests.test_learning_model_shadow import _pipeline

    cfg = _config_dir(tmp_path, None)
    _rank_all_filters(cfg)
    result = _pipeline(tmp_path / "jobs.db", cfg)
    by_lane: dict[str, set[str]] = {}
    for r in result.scored_jobs:
        by_lane.setdefault(r.lane, set()).add(r.job_id)
        assert (r.lane == L.MAIN) == (not r.weak_flags)
    assert len(by_lane.get(L.OUTSIDE, set())) <= 10
    conn = sqlite3.connect(tmp_path / "jobs.db")
    lanes = {jid: (json.loads(e) or {}).get("lane") for jid, e in conn.execute(
        "SELECT job_id, exposure_json FROM job_decisions WHERE run_id = 'run' AND outcome = 'accepted'")}
    conn.close()
    assert lanes == {r.job_id: r.lane for r in result.scored_jobs}


def test_api_keeps_weak_offers_out_of_the_main_list(isolated_app):
    from fastapi.testclient import TestClient

    from src.models.job import Job
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db = isolated_app.state.db_path
    with TestClient(isolated_app) as client:
        s = Storage(db_path=db)
        for j in ["main", "lane1", "lane2", "below"]:
            s.upsert_job(Job(id=j, source="test", source_job_id=j, canonical_url=f"https://example.test/{j}",
                             title=f"Développeur {j}", company="ACME", description="Poste.", location="Lyon",
                             content_hash=f"hash-{j}"))
        s.close()
        rs = RunStorage(db_path=db)
        rs.init_db()
        rs.create_run("r1")
        rs.store_job_scores("r1", [
            {"job_id": "main", "final_score": 0.5, "score_breakdown": _bd(weak=False)},
            {"job_id": "lane1", "final_score": 0.8, "score_breakdown": _bd()},
            {"job_id": "lane2", "final_score": 0.7, "score_breakdown": _bd()},
            {"job_id": "below", "final_score": 0.9, "score_breakdown": _bd(domain=0.3)},
        ])
        rs.complete_run("r1", {})
        rs.close()
        assert [j["id"] for j in client.get("/api/jobs").json()] == ["main"]
        lane = client.get("/api/jobs/outside-criteria").json()
        assert [j["id"] for j in lane] == ["lane1", "lane2"]
        assert lane[0]["score"]["weak_flags"][0]["code"] == "published_age"
        assert set(lane[0]["score"]["factors"]) == {"domain_relevance"}           # factors stay numeric
        assert client.get("/api/jobs?min_tier=strong").json() == []               # the tray never counts them
        found = {j["id"] for j in client.get("/api/jobs?q=Développeur").json()}
        assert {"lane1", "below"} <= found                                         # search still finds them
        client.post("/api/jobs/lane1/feedback", json={"type": "hidden", "source": "ui"})
        assert [j["id"] for j in client.get("/api/jobs/outside-criteria").json()] == ["lane2"]


@pytest.mark.parametrize("path", ["/api/jobs/outside-criteria"])
def test_lane_endpoint_without_runs(isolated_app, path):
    from fastapi.testclient import TestClient

    with TestClient(isolated_app) as client:
        assert client.get(path).json() == []
