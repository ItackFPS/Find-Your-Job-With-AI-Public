"""Pause learning and Reset learning (ADR 0015, v5 §4.12 four-actions table)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from src.feedback.service import FeedbackService
from src.feedback.storage import FeedbackStorage
from src.privacy import learning
from src.storage.data_classes import JOBS_DB_TABLES, RESET_CLEARED_TABLES
from tests.test_feedback_reaches_personalization import _around_one_answer, _pipeline


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_reset_cleared_tables_are_classified():
    assert set(RESET_CLEARED_TABLES) <= set(JOBS_DB_TABLES)
    assert "feedback_events" not in RESET_CLEARED_TABLES
    assert "exposure_log" not in RESET_CLEARED_TABLES


def test_pause_skips_personalization_and_resume_restores_it(tmp_path):
    before, after, db = _around_one_answer(tmp_path, True)
    assert any(s.personalization_adjustment > 0 for s in after.scored_jobs)
    learning.set_paused(str(db), True)
    cfg = tmp_path / "config"
    paused = _pipeline(db, cfg)
    assert all(s.personalization_adjustment == 0 for s in paused.scored_jobs)
    assert [(s.job_id, s.final_score) for s in paused.scored_jobs] == \
        [(s.job_id, s.final_score) for s in before.scored_jobs]
    # signals are still recorded while paused
    FeedbackService(str(db), config_dir=str(cfg)).record_list(before.scored_jobs[1].job_id, "saved")
    assert _count(db, "feedback_events") == 2
    learning.set_paused(str(db), False)
    resumed = _pipeline(db, cfg)
    assert any(s.personalization_adjustment > 0 for s in resumed.scored_jobs)


def test_reset_clears_derived_state_keeps_signals_and_hidden(tmp_path):
    before, after, db = _around_one_answer(tmp_path, True)
    cfg = tmp_path / "config"
    hidden = before.scored_jobs[-1].job_id
    FeedbackService(str(db), config_dir=str(cfg)).record_list(hidden, "hidden")
    assert _count(db, "job_feedback") == 1
    events = _count(db, "feedback_events")

    at, cleared = learning.reset(str(db))
    assert at and cleared["job_feedback"] == 1
    assert _count(db, "job_feedback") == 0
    assert _count(db, "feedback_events") == events          # journal kept
    store = FeedbackStorage(str(db))
    store.init_db()
    try:
        assert store.hidden_job_ids() == {hidden}             # hide is list management, not learning
    finally:
        store.close()

    # the next run learns nothing from pre-reset signals, even when the bridge is rebuilt
    FeedbackService(str(db), config_dir=str(cfg)).sync_bridge()
    assert _count(db, "job_feedback") == 0
    nxt = _pipeline(db, cfg)
    assert all(s.personalization_adjustment == 0 for s in nxt.scored_jobs)

    # a signal after the marker counts again
    FeedbackService(str(db), config_dir=str(cfg)).record_explicit(before.scored_jobs[0].job_id, "interested",
                                                                   source="ui")
    assert _count(db, "job_feedback") == 1


def test_after_reset_compares_instants():
    assert learning.after_reset("2026-11-05T10:00:01+00:00", "2026-11-05T10:00:00+00:00")
    assert not learning.after_reset("2026-11-05T09:59:59+00:00", "2026-11-05T10:00:00+00:00")
    assert learning.after_reset("2026-11-05T12:00:00+02:00", "2026-11-05T09:30:00+00:00")
    assert learning.after_reset("anything", None)


def test_privacy_api(isolated_app):
    with TestClient(isolated_app) as client:
        assert client.get("/api/privacy/learning").json() == {"paused": False, "reset_at": None}
        assert client.put("/api/privacy/learning/pause", json={"paused": True}).json()["paused"] is True
        assert client.get("/api/session").json()["learning_paused"] is True
        assert client.post("/api/privacy/learning/reset", json={}).status_code == 400
        r = client.post("/api/privacy/learning/reset", json={"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["reset_at"] and body["paused"] is True
        assert set(body["cleared"]) <= set(RESET_CLEARED_TABLES)
