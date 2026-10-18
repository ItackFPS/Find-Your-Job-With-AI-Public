"""Career Skill Graph: demand among relevant jobs, co-occurrence, gaps with evidence — read-only."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from src.models.job import Job
from src.skillgraph.graph import build_skill_graph
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def _db(path: Path) -> Path:
    s = Storage(str(path))
    s.init_db()
    recent = NOW - timedelta(days=5)
    jobs = [
        # relevant (high score): python + docker + kubernetes
        *[Job(id=f"hi{i}", title=f"Dev Python {i}", skills=["Python", "Docker", "Kubernetes"],
              first_seen_at=recent, discovered_at=recent) for i in range(3)],
        # relevant (user acted): python + aws
        Job(id="acted", title="Cloud dev", skills=["python", "AWS"], first_seen_at=recent, discovered_at=recent),
        # not relevant: low score, php only (+ ROME noise that is not a technology)
        *[Job(id=f"lo{i}", title=f"PHP {i}", skills=["PHP", "Configurer le poste de travail"],
              first_seen_at=recent, discovered_at=recent) for i in range(4)],
        # outside the window
        Job(id="old", title="Old Rust", skills=["Rust"], first_seen_at=NOW - timedelta(days=200),
            discovered_at=NOW - timedelta(days=200)),
    ]
    s.upsert_jobs_batch(jobs)
    s.update_application_status("acted", "applied")
    s.close()
    rs = RunStorage(str(path))
    rs.init_db()
    rs.create_run("r1")
    rs.store_job_scores("r1", [{"job_id": j.id, "final_score": 0.8 if j.id.startswith("hi") else 0.3} for j in jobs])
    rs.close()
    return path


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_graph_ranks_relevant_demand_and_finds_gaps_with_evidence(tmp_path):
    db = _db(tmp_path / "jobs.db")
    before = _sha(db)
    g = build_skill_graph(db, ["Python"], now=NOW)
    assert _sha(db) == before                                         # read-only
    assert (g["n_jobs"], g["n_relevant"]) == (8, 4)
    nodes = {n["skill"]: n for n in g["nodes"]}
    assert nodes["python"]["owned"] and nodes["python"]["relevant_demand"] == 4
    assert nodes["php"]["relevant_demand"] == 0 and nodes["php"]["demand"] == 4
    assert "rust" not in nodes and not any("configurer" in s for s in nodes)   # window + vocabulary
    gaps = {gp["skill"]: gp for gp in g["gaps"]}
    assert list(gaps)[:2] == ["docker", "kubernetes"] and "php" not in gaps and "aws" not in gaps   # aws: 1 < support
    assert gaps["docker"]["adjacent_owned"] == [{"skill": "python", "jobs": 3}]
    assert "3 of 4 relevant" in gaps["docker"]["why"] and gaps["docker"]["example_titles"]
    edges = {(e["source"], e["target"]): e["weight"] for e in g["edges"]}
    assert edges[("docker", "kubernetes")] == 3 and edges[("docker", "python")] == 3
    assert ("php", "python") not in edges                               # never tagged on the same job
    assert any("relevant job(s)" in x for x in g["limitations"])       # small sample stated


def test_missing_database_gives_an_empty_graph_not_an_error(tmp_path):
    g = build_skill_graph(tmp_path / "none.db", ["python"])
    assert g["nodes"] == [] and "no database yet" in g["limitations"]


def test_api_serves_the_graph_for_the_configured_profile(tmp_path):
    from src.api.app import app

    db = _db(tmp_path / "jobs.db")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "profile.yaml").write_text("target_skills: [Python, Docker]\n", encoding="utf-8")
    saved = (app.state.db_path, app.state.config_dir)
    app.state.db_path, app.state.config_dir = str(db), str(cfg)
    try:
        body = TestClient(app).get("/api/skills/graph", params={"days": 365}).json()
    finally:
        app.state.db_path, app.state.config_dir = saved
    assert body["owned"] == ["docker", "python"]
    assert [g["skill"] for g in body["gaps"]][:1] == ["kubernetes"]


def test_no_sqlite_write_connection_is_ever_opened(tmp_path, monkeypatch):
    db = _db(tmp_path / "jobs.db")
    seen: list[str] = []
    real = sqlite3.connect

    def spy(database, *a, **k):
        seen.append(str(database))
        return real(database, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    build_skill_graph(db, ["python"], now=NOW)
    assert seen and all("mode=ro" in s for s in seen)
