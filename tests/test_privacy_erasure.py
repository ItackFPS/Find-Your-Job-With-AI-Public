"""Erase my activity: resumable, verified erasure job (ADR 0015)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.feedback.exposure import ExposureItem, ExposureStorage
from src.feedback.service import Delivery, FeedbackService
from src.privacy import erasure
from src.storage import app_state
from src.storage.data_classes import JOBS_DB_TABLES
from src.storage.migrations import backup_database
from src.storage.sqlite import Storage
from tests.test_feedback_delivery import _job


def _seed(db: str) -> list[str]:
    """Jobs, a journal, exposures, a tracker status, an old backup and calibration answers."""
    s = Storage(db_path=db)
    s.init_db()
    for j in ("j1", "j2", "j3"):
        s.upsert_job(_job(j))
    s.update_application_status("j1", "applied")
    s.close()
    svc = FeedbackService(db)
    ids = [svc.record_list("j1", "saved").event_id, svc.record_list("j2", "hidden").event_id,
           svc.record_explicit("j3", "interested", source="ui").event_id]
    ex = ExposureStorage(db)
    ex.init_db()
    try:
        assert ex.record_batch(app_state.current(db).token, "sess-1",
                               [ExposureItem("j1", "jobs", position=1, visible_ms=1500)]) == 1
    finally:
        ex.close()
    conn = sqlite3.connect(db)
    try:
        backup_database(conn, Path(db), "test", now=datetime(2026, 1, 1, tzinfo=UTC))
    finally:
        conn.close()
    cal = Path(db).parent / "calibration" / "real-v2"
    cal.mkdir(parents=True)
    (cal / "calibration_answers.jsonl").write_text('{"label": 1}\n', encoding="utf-8")
    return ids


def _count(db: str, sql: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "jobs.db")
    _seed(path)
    return path


def test_erasure_job_table_is_control():
    assert JOBS_DB_TABLES["erasure_jobs"] == "control"
    assert "feedback_events" in erasure.scope_tables() and "jobs" not in erasure.scope_tables()


def test_full_erasure_reaches_complete_and_verifies(db):
    old = app_state.current(db)
    erasure.request(db, erasure.Scope())
    assert erasure.pending(db)
    st = erasure.run(db)
    assert st["state"] == "COMPLETE", st
    assert not st["pending"] and not erasure.pending(db)
    assert st["verification"] == {"rows": True, "wal": True, "bytes": True, "copies": True, "generation": True}
    assert st["counts"]["feedback_events"] == 3 and st["counts"]["jobs.application_status"] == 1
    assert st["inventory"]["sampled_ids"] == 3
    present = {r for r in erasure.scope_tables()
               if _count(db, f"SELECT COUNT(*) FROM sqlite_master WHERE name = '{r}'")}
    assert {"feedback_events", "exposure_log"} <= present
    for table in present:
        assert _count(db, f"SELECT COUNT(*) FROM {table}") == 0
    assert _count(db, "SELECT COUNT(*) FROM jobs") == 3                         # offers stay
    assert _count(db, "SELECT COUNT(*) FROM jobs WHERE application_status IS NOT NULL") == 0
    assert not list((Path(db).parent / "backups").glob("*.db"))
    assert not (Path(db).parent / "calibration").exists()
    assert app_state.current(db).erasure_generation == old.erasure_generation + 1
    # the journal is append-only again
    FeedbackService(db).record_list("j2", "saved")
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM feedback_events")
    finally:
        conn.close()


def test_write_stamped_before_erasure_is_refused(db):
    old = app_state.current(db).token
    erasure.request(db, erasure.Scope())
    erasure.run(db)
    from src.storage.app_state import GenerationMismatch
    with pytest.raises(GenerationMismatch):
        FeedbackService(db).record_list("j1", "saved", delivery=Delivery(generation_token=old))
    assert _count(db, "SELECT COUNT(*) FROM feedback_events") == 0


def test_keep_tracker_and_calibration_options(db):
    erasure.request(db, erasure.Scope(keep_tracker=True, erase_calibration=False))
    st = erasure.run(db)
    assert st["state"] == "COMPLETE", st
    assert _count(db, "SELECT COUNT(*) FROM jobs WHERE application_status = 'applied'") == 1
    assert (Path(db).parent / "calibration" / "real-v2" / "calibration_answers.jsonl").exists()
    assert _count(db, "SELECT COUNT(*) FROM feedback_events") == 0


@pytest.mark.parametrize("crash_after", erasure.STATES[1:-1])
def test_resumes_after_a_crash_at_every_step(db, crash_after):
    erasure.request(db, erasure.Scope())
    st = erasure.run(db, stop_after=crash_after)
    assert st["state"] == crash_after and erasure.pending(db)
    st = erasure.run(db)
    assert st["state"] == "COMPLETE", st
    assert all(st["verification"].values())
    assert st["attempts"] == 2
    assert app_state.current(db).erasure_generation == 1                    # incremented once


def test_crash_inside_db_erased_rolls_back(db, monkeypatch):
    erasure.request(db, erasure.Scope())
    erasure.run(db, stop_after="INVENTORIED")

    def boom(conn):
        raise RuntimeError("power cut")
    monkeypatch.setattr(app_state, "increment_generation", boom)
    with pytest.raises(RuntimeError):
        erasure.run(db)
    assert _count(db, "SELECT COUNT(*) FROM feedback_events") == 3            # nothing half-erased
    assert _count(db, "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'feedback_events_no_%'") == 2
    monkeypatch.undo()
    assert erasure.run(db)["state"] == "COMPLETE"


def test_blocked_keeps_last_step_and_retry_finishes(db):
    erasure.request(db, erasure.Scope())

    def busy():
        raise erasure.Blocked("pipeline_busy: test")
    st = erasure.run(db, quiesce=busy)
    assert st["state"] == "BLOCKED" and st["last_state"] == "REQUESTED"
    assert st["last_error"].startswith("pipeline_busy")
    assert _count(db, "SELECT COUNT(*) FROM feedback_events") == 3
    st = erasure.run(db)
    assert st["state"] == "COMPLETE" and st["last_error"] is None


def test_backup_refused_while_pending(db):
    erasure.request(db, erasure.Scope())
    conn = sqlite3.connect(db)
    try:
        assert backup_database(conn, Path(db), "migration") is None
    finally:
        conn.close()


def test_verification_fails_when_a_sampled_id_survives(db, monkeypatch):
    erasure.request(db, erasure.Scope())
    erasure.run(db, stop_after="COPIES_REMOVED")
    survivor = erasure._read_marker(db)["sample"][0]
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("UPDATE jobs SET description = ? WHERE id = 'j3'", (survivor,))
    finally:
        conn.close()
    st = erasure.run(db)
    assert st["state"] == "BLOCKED" and st["last_error"] == "verification_failed: bytes"
    assert erasure.pending(db)


def test_api_erase_flow_and_423(isolated_app):
    with TestClient(isolated_app) as client:
        db = isolated_app.state.db_path
        _seed(db)
        isolated_app.state.erasure_sync = True
        assert client.get("/api/privacy/erase").json()["state"] is None
        assert client.post("/api/privacy/erase", json={}).status_code == 400
        # a pending erasure locks writes but not the privacy routes
        erasure.request(db, erasure.Scope())
        r = client.put("/api/privacy/learning/pause", json={"paused": True})
        assert r.status_code == 200
        r = client.post("/api/jobs/j1/feedback", json={"type": "interested", "source": "ui"})
        assert r.status_code == 423 and r.json()["detail"] == "erasure_running"
        assert client.post("/api/privacy/erase", json={"confirm": True}).status_code == 409
        r = client.post("/api/privacy/erase/retry")
        assert r.status_code == 202
        st = client.get("/api/privacy/erase").json()
        assert st["state"] == "COMPLETE" and st["pending"] is False and st["running"] is False
        assert client.post("/api/privacy/erase/retry").status_code == 409
        # a new erasure through the API
        r = client.post("/api/privacy/erase", json={"confirm": True, "keep_tracker": True})
        assert r.status_code == 202 and r.json()["state"] == "COMPLETE"
        assert r.headers["X-Erasure-Generation"].endswith(":2")
        assert client.post("/api/jobs/j1/feedback", json={"type": "interested", "source": "ui"}).status_code == 201


def test_startup_resumes_a_pending_erasure(isolated_app):
    db = isolated_app.state.db_path
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    _seed(db)
    erasure.request(db, erasure.Scope())
    erasure.run(db, stop_after="QUIESCED")
    with TestClient(isolated_app) as client:
        assert client.get("/api/privacy/erase").json()["state"] == "COMPLETE"
    assert _count(db, "SELECT COUNT(*) FROM feedback_events") == 0
