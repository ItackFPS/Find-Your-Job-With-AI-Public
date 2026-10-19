"""Feedback API + mirroring of outcome/behaviour events; existing contracts unchanged."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models.job import Job
from src.storage.sqlite import Storage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def client(tmp_db: str, tmp_path: Path):
    from src.api.app import app

    cfg = tmp_path / "config"
    cfg.mkdir()
    saved = (app.state.db_path, getattr(app.state, "config_dir", None))
    app.state.db_path = tmp_db
    app.state.config_dir = str(cfg)
    yield TestClient(app)
    app.state.db_path, app.state.config_dir = saved          # never leak state into other modules


@pytest.fixture
def job(tmp_db: str) -> Job:
    s = Storage(db_path=tmp_db)
    s.init_db()
    j = Job(source="t", source_job_id="J1", title="Software Engineer", company="Acme", location="Paris",
            content_hash="h", description="d" * 300)
    s.upsert_job(j)
    s.close()
    return j


def test_vocabulary_is_served(client):
    body = client.get("/api/jobs/feedback/vocabulary").json()
    assert set(body["explicit_types"]) == {"interested", "not_interested", "ranking_feedback", "unsure"}
    assert "too_far" in body["allowed_reasons"]["not_interested"]
    assert "withdrawn" in body["canonical_statuses"]


def test_unsure_answer_is_recorded_as_explicit_feedback(client, job):
    r = client.post(f"/api/jobs/{job.id}/feedback", json={"type": "unsure", "reasons": []})
    assert r.status_code == 201
    assert r.json()["signal_class"] == "explicit" and r.json()["type"] == "unsure"


def test_post_and_list_feedback(client, job):
    r = client.post(f"/api/jobs/{job.id}/feedback", json={"type": "not_interested", "reasons": ["too_far"], "note": "n"})
    assert r.status_code == 201
    body = r.json()
    assert body["signal_class"] == "explicit" and body["source"] == "api" and body["reasons"] == ["too_far"]
    client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested"})
    events = client.get(f"/api/jobs/{job.id}/feedback").json()
    assert [e["type"] for e in events] == ["interested", "not_interested"]          # newest first


def test_provenance_is_recorded_from_the_caller(client, job):
    ui = client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested", "source": "ui"}).json()
    api = client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested"}).json()
    assert (ui["source"], api["source"]) == ("ui", "api")
    assert client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested", "source": "import"}).status_code == 422


def test_client_can_pin_the_decision_it_displayed(client, job, tmp_db):
    from src.decisions.models import DecisionRecord, JobMeta
    from src.decisions.storage import DecisionStorage

    st = DecisionStorage(tmp_db)
    st.init_db()
    st.write_run([DecisionRecord(decision_id="dec-1", run_id="r1", job_id=job.id, decided_at="2026-01-01T00:00:00+00:00",
                                 outcome="accepted", deciding_stage="ranking", strategy_version="sv-1", job=JobMeta())], {})
    st.close()
    ok = client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested", "decision_id": "dec-1"})
    assert ok.status_code == 201 and ok.json()["decision_id"] == "dec-1" and ok.json()["strategy_version"] == "sv-1"
    assert client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested", "decision_id": "x"}).status_code == 422


def test_feedback_list_is_bounded(client, job):
    for _ in range(3):
        client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested"})
    assert len(client.get(f"/api/jobs/{job.id}/feedback?limit=2").json()) == 2
    assert client.get(f"/api/jobs/{job.id}/feedback?limit=0").status_code == 422


@pytest.mark.parametrize("payload", [
    {"type": "nonsense"}, {"type": "interested", "reasons": ["too_far"]}, {"type": "interested", "note": "x" * 501},
    {"type": "x" * 41}, {"type": "not_interested", "reasons": ["other"] * 13},
])
def test_invalid_feedback_is_422(client, job, payload):
    assert client.post(f"/api/jobs/{job.id}/feedback", json=payload).status_code == 422
    assert client.get(f"/api/jobs/{job.id}/feedback").json() == []


def test_unknown_job_is_404(client):
    assert client.post("/api/jobs/nope/feedback", json={"type": "interested"}).status_code == 404
    assert client.get("/api/jobs/nope/feedback").status_code == 404


def test_behaviour_events_are_mirrored_without_changing_the_existing_contract(client, job):
    r = client.post(f"/api/jobs/{job.id}/events", json={"event_type": "viewed"})
    assert r.status_code == 201 and set(r.json()) >= {"id", "job_id", "event_type", "occurred_at"}
    client.post(f"/api/jobs/{job.id}/events", json={"event_type": "apply_link_opened"})
    events = client.get(f"/api/jobs/{job.id}/feedback").json()
    assert {(e["signal_class"], e["type"]) for e in events} == {("behavior", "viewed"), ("behavior", "apply_link_opened")}
    assert len(client.get(f"/api/jobs/{job.id}/events").json()) == 2                 # outcome_events untouched


def test_status_changes_are_mirrored_with_legacy_and_canonical_values(client, job):
    assert client.patch(f"/api/jobs/{job.id}/application-status", json={"application_status": "applied"}).status_code == 200
    client.patch(f"/api/jobs/{job.id}/application-status", json={"application_status": "denied"})
    client.patch(f"/api/jobs/{job.id}/application-status", json={"application_status": "denied"})   # no-op repeat
    client.patch(f"/api/jobs/{job.id}/application-status", json={"application_status": "withdrawn"})
    outcome = [e for e in client.get(f"/api/jobs/{job.id}/feedback").json() if e["signal_class"] == "outcome"]
    assert [(e["metadata"]["legacy_to"], e["metadata"]["canonical_to"]) for e in reversed(outcome)] == [
        ("applied", "applied"), ("denied", None), ("withdrawn", "withdrawn")]
    assert all(e["metadata"]["outcome_event_id"] for e in outcome)             # tied to outcome_events rows
    from src.feedback.service import FeedbackService
    assert FeedbackService(client.app.state.db_path, client.app.state.config_dir).backfill_from_outcome_events() == (0, 0)
    assert client.patch(f"/api/jobs/{job.id}/application-status", json={"application_status": "bogus"}).status_code == 422


def test_feedback_failure_never_breaks_the_original_request(client, job, monkeypatch):
    import src.feedback.service as svc

    monkeypatch.setattr(svc.FeedbackService, "record_behavior", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(svc.FeedbackService, "record_status_change", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert client.post(f"/api/jobs/{job.id}/events", json={"event_type": "viewed"}).status_code == 201
    assert client.patch(f"/api/jobs/{job.id}/application-status", json={"application_status": "applied"}).status_code == 200


def test_explicit_feedback_does_not_change_rankings_by_default(client, job, tmp_db):
    assert client.post(f"/api/jobs/{job.id}/feedback", json={"type": "interested"}).status_code == 201
    conn = sqlite3.connect(tmp_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "feedback_events" in tables                         # the journal got the event…
    assert "job_feedback" not in tables or conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 0
