"""Self-monitoring base (ADR 0025, v5 L2.3): metrics, the two detectors on injected faults, alerts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.models.job import Job
from src.ops import monitor
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

T0 = datetime(2026, 11, 1, 8, tzinfo=UTC)


def _runs_schema(path: str) -> None:
    rs = RunStorage(db_path=path)
    rs.init_db()
    rs.close()


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "jobs.db")
    Storage(db_path=path).init_db()
    _runs_schema(path)
    return path


def _run(db: str, i: int, at: datetime, by_source: dict) -> str:
    conn = sqlite3.connect(db)
    run_id = f"r{i}"
    conn.execute("INSERT INTO pipeline_runs (id, started_at, completed_at, status, stats_json) "
                 "VALUES (?, ?, ?, 'completed', ?)",
                 (run_id, at.isoformat(), at.isoformat(), json.dumps({"by_source": by_source})))
    conn.commit()
    conn.close()
    return run_id


def _jobs(db: str, at: datetime, n: int, *, company: bool = True, source: str = "remotive") -> None:
    st = Storage(db_path=db)
    for k in range(n):
        st.upsert_job(Job(id=f"{source}-{at:%m%d}-{k}", source=source, title="Dev", description="d",
                          company="Acme" if company else None, location="Paris",
                          first_seen_at=at, last_seen_at=at))


def _open(db: str) -> list[dict]:
    return monitor.alerts(db)


def test_robust_z():
    assert monitor.robust_z(5, [10, 11, 9, 10, 12]) < -3
    assert monitor.robust_z(10, [10, 10, 10]) is None


def test_volume_drop_needs_two_runs_then_resolves(db):
    for d in range(7):
        at = T0 + timedelta(days=d)
        monitor.after_run(db, _run(db, d, at, {"remotive": {"collected": 40 + d % 3, "status": "ok"}}), now=at)
    assert _open(db) == []
    at = T0 + timedelta(days=7)
    monitor.after_run(db, _run(db, 7, at, {"remotive": {"collected": 0}}), now=at)
    assert _open(db) == []                                   # one bad run is not enough
    at = T0 + timedelta(days=8)
    monitor.after_run(db, _run(db, 8, at, {"remotive": {"collected": 0}}), now=at)
    [alert] = _open(db)
    assert (alert["code"], alert["scope"]) == ("source_volume_drop", "source:remotive")
    assert alert["evidence"]["recent"] == [0.0, 0.0] and alert["evidence"]["median"] >= 40
    at = T0 + timedelta(days=9)
    monitor.after_run(db, _run(db, 9, at, {"remotive": {"collected": 41}}), now=at)
    monitor.after_run(db, _run(db, 10, at + timedelta(hours=1), {"remotive": {"collected": 40}}), now=at)
    assert _open(db) == []
    [resolved] = monitor.alerts(db, ("resolved",))
    assert resolved["resolved_by"] == "auto"


def test_skipped_and_paused_sources_are_not_measured(db):
    for d in range(9):
        at = T0 + timedelta(days=d)
        status = "paused" if d >= 7 else "ok"
        monitor.after_run(db, _run(db, d, at, {"x": {"collected": 0 if d >= 7 else 30, "status": status}}), now=at)
    assert _open(db) == []


def test_field_completeness_drop(db):
    for d in range(6):
        at = T0 + timedelta(days=d)
        _jobs(db, at, 12)
        monitor.after_run(db, _run(db, d, at, {}), now=at)
    assert _open(db) == []
    at = T0 + timedelta(days=6)
    _jobs(db, at, 12, company=False)                         # the source stopped giving a company
    monitor.after_run(db, _run(db, 6, at, {}), now=at)
    [alert] = _open(db)
    assert alert["code"] == "field_completeness_drop"
    assert alert["scope"] == "source:remotive:field:company"
    assert alert["evidence"]["today"] == 0.0 and alert["evidence"]["median"] == 1.0


def test_user_resolves_and_api(isolated_app):
    from fastapi.testclient import TestClient

    with TestClient(isolated_app) as client:
        path = isolated_app.state.db_path
        _runs_schema(path)
        for d in range(9):
            at = T0 + timedelta(days=d)
            monitor.after_run(path, _run(path, d, at, {"s": {"collected": 0 if d >= 7 else 50}}), now=at)
        [alert] = client.get("/api/quality/alerts").json()["alerts"]
        assert alert["code"] == "source_volume_drop"
        assert client.post(f"/api/quality/alerts/{alert['alert_id']}/resolve").status_code == 200
        assert client.get("/api/quality/alerts").json()["alerts"] == []
        assert client.post(f"/api/quality/alerts/{alert['alert_id']}/resolve").status_code == 404
        assert len(client.get("/api/quality/alerts?status=all").json()["alerts"]) == 1



def _start_and_wait(db: str, monkeypatch, after_run) -> str:
    import time
    from unittest.mock import patch

    from src.api.runner import PipelineRunner
    from src.pipeline.orchestrator import PipelineResult, PipelineStats

    monkeypatch.setattr(monitor, "after_run", after_run)
    now = datetime.now(UTC)
    result = PipelineResult(run_id="x", started_at=now, completed_at=now, status="completed",
                            stats=PipelineStats(), scored_jobs=[], rejection_records=[])
    runner = PipelineRunner(db_path=db)
    with patch("src.api.runner.run_pipeline", return_value=result):
        run_id = runner.start_run(use_ai=False)
        deadline = time.time() + 5.0
        while runner.is_running() and time.time() < deadline:
            time.sleep(0.01)
    return run_id


def test_runner_calls_the_monitor_after_a_completed_run(db, monkeypatch):
    seen: list[str] = []
    run_id = _start_and_wait(db, monkeypatch, lambda path, rid, **k: seen.append(rid))
    assert seen == [run_id]


def test_a_crashing_monitor_does_not_fail_the_run(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("detector crashed")
    run_id = _start_and_wait(db, monkeypatch, boom)
    rs = RunStorage(db_path=db)
    row = rs.get_run(run_id)
    rs.close()
    assert row["status"] == "completed"
