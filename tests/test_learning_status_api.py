"""Personal ranking status and "Reprendre" (ADR 0019; stage L4.4)."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from src.learning import health as H


def _health(path: str, state: str, strata: dict | None = None, flags: dict | None = None) -> None:
    conn = sqlite3.connect(path)
    H.ensure_schema(conn)
    with conn:
        conn.execute("INSERT OR REPLACE INTO personal_health (id, state, state_since, cap_factor, halvings_json, "
                     "release_key, decision_json, flags_json, last_event, run_id, updated_at) "
                     "VALUES (1, ?, '2026-09-01T00:00:00+00:00', 1.0, '[]', 'k', ?, ?, 'x', 'r', "
                     "'2026-09-01T00:00:00+00:00')",
                     (state, json.dumps({"strata": strata or {}}), json.dumps(flags or {})))
    conn.close()


def test_no_record_is_observing(isolated_app):
    with TestClient(isolated_app) as client:
        body = client.get("/api/privacy/learning/status").json()
    assert body["state"] == "shadow" and body["wording_key"] == "learning.status.observing"
    assert body["paused_notice"] is False and body["activation_enabled"] is False and body["strata"] == {}


def test_status_reports_strata_and_flags(isolated_app):
    _health(isolated_app.state.db_path, "shadow", {"LB": {"discordant": 3, "wins": 2}, "PF": {"discordant": 5, "wins": 1}},
            {"activation_enabled": False})
    with TestClient(isolated_app) as client:
        body = client.get("/api/privacy/learning/status").json()
    assert body["strata"] == {"LB": {"discordant": 3, "challenger_wins": 2}, "PF": {"discordant": 5, "challenger_wins": 1}}


def test_paused_shows_the_notice_and_resume_returns_to_observation(isolated_app):
    db = isolated_app.state.db_path
    _health(db, "paused")
    with TestClient(isolated_app) as client:
        body = client.get("/api/privacy/learning/status").json()
        assert body["paused_notice"] is True and body["wording_key"] == "learning.status.paused"
        r = client.post("/api/privacy/learning/resume")
        assert r.status_code == 200 and r.json()["state"] == "shadow" and r.json()["last_event"] == "resumed"
        assert client.post("/api/privacy/learning/resume").status_code == 409


def test_reset_clears_the_health_record(isolated_app):
    db = isolated_app.state.db_path
    _health(db, "paused")
    with TestClient(isolated_app) as client:
        assert client.post("/api/privacy/learning/reset", json={"confirm": True}).status_code == 200
        assert client.get("/api/privacy/learning/status").json()["state"] == "shadow"
