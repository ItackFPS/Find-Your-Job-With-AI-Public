"""Pause learning and Reset learning (ADR 0015, v5 §4.12 four-actions table)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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


def _rows(db: Path, sql: str, *args) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:          # table not created yet
        return 0
    finally:
        conn.close()


def test_pause_stops_exactly_what_the_settings_text_says(tmp_path):
    """Owner decision D2 part 2 (Phase B B9-A), one assertion per component of the Settings text:
    stopped = personal reordering; not stopped = recording of answers, Save / Hide and exposures."""
    from src.feedback.exposure import ExposureItem, ExposureStorage
    from src.storage import app_state

    before, _after, db = _around_one_answer(tmp_path, True)
    cfg = tmp_path / "config"
    learning.set_paused(str(db), True)
    paused = _pipeline(db, cfg)
    # stopped: personal reordering
    assert all(s.personalization_adjustment == 0 for s in paused.scored_jobs)
    # not stopped: answers and list actions
    n = _count(db, "feedback_events")
    service = FeedbackService(str(db), config_dir=str(cfg))
    service.record_explicit(before.scored_jobs[1].job_id, "not_interested", source="ui")
    service.record_list(before.scored_jobs[2].job_id, "saved")
    assert _count(db, "feedback_events") == n + 2
    # not stopped: visible exposures
    store = ExposureStorage(str(db))
    store.init_db()
    try:
        assert store.record_batch(app_state.current(str(db)).token, "s1",
                                  [ExposureItem(before.scored_jobs[0].job_id, "jobs", position=0)]) == 1
    finally:
        store.close()


def test_the_settings_text_names_what_pause_stops_and_keeps():
    import json

    root = Path(__file__).resolve().parents[1] / "frontend" / "src" / "locales"
    en = json.loads((root / "en" / "translation.json").read_text(encoding="utf-8"))["settings"]["learning"]["pauseHelp"]
    fr = json.loads((root / "fr" / "translation.json").read_text(encoding="utf-8"))["settings"]["learning"]["pauseHelp"]
    for word in ("ranking stops adapting", "still recorded", "Erase"):
        assert word in en, word
    for word in ("cesse de s’adapter", "restent enregistrées", "effacement"):
        assert word in fr, word
    # the personal learning module was removed (#250): nothing it did is promised any more
    for gone in ("personal ranking", "Découverte"):
        assert gone not in en, gone
    for gone in ("classement personnel", "Découverte"):
        assert gone not in fr, gone


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


def test_reset_marker_separates_same_second_signals(tmp_path):
    db = tmp_path / "jobs.db"
    learning.state(str(db))
    before = datetime.now(UTC).isoformat(timespec="seconds")      # a signal in the same second
    at, _ = learning.reset(str(db))
    after = datetime.now(UTC).isoformat(timespec="seconds")       # a signal right after the call
    assert not learning.after_reset(before, at)
    assert learning.after_reset(after, at)


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
