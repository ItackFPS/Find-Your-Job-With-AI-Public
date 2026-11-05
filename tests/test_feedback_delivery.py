"""Ordered, idempotent, generation-checked delivery of durable events (ADR 0015, ADR 0016)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.feedback.service import Delivery, FeedbackService, client_time_trusted
from src.feedback.storage import FeedbackStorage
from src.models.job import Job
from src.storage import app_state
from src.storage.sqlite import Storage


def _job(job_id: str) -> Job:
    return Job(id=job_id, source="test", source_job_id=job_id, canonical_url=f"https://example.test/{job_id}",
               title="Développeur Python", company="ACME", description="Poste de développeur.",
               location="Lyon")


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "jobs.db")
    s = Storage(db_path=path)
    s.init_db()
    for j in ("j1", "j2"):
        s.upsert_job(_job(j))
    s.close()
    return path


def _token(db: str) -> str:
    return app_state.current(db).token


def _rows(db: str, job_id: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM feedback_events WHERE job_id = ? ORDER BY rowid", (job_id,)).fetchall()
    finally:
        conn.close()


def test_new_database_gets_an_epoch_and_generation_zero(db):
    st = app_state.current(db)
    uuid.UUID(st.data_epoch)
    assert st.erasure_generation == 0
    assert app_state.current(db).data_epoch == st.data_epoch  # stable across reads


def test_replayed_client_event_inserts_one_row(db):
    svc = FeedbackService(db, "config-missing")
    d = Delivery(client_event_id="evt-0001-abcd", generation_token=_token(db))
    first = svc.record_list("j1", "saved", delivery=d)
    second = svc.record_list("j1", "saved", delivery=d)
    assert second.event_id == first.event_id
    assert len(_rows(db, "j1")) == 1
    assert _rows(db, "j1")[0]["generation"] == 0


def test_stale_generation_or_epoch_is_refused(db):
    svc = FeedbackService(db, "config-missing")
    epoch = app_state.current(db).data_epoch
    with pytest.raises(app_state.GenerationMismatch):
        svc.record_list("j1", "hidden", delivery=Delivery(generation_token=f"{epoch}:1"))
    with pytest.raises(app_state.GenerationMismatch):
        svc.record_list("j1", "hidden", delivery=Delivery(generation_token=f"{uuid.uuid4()}:0"))
    with pytest.raises(app_state.GenerationMismatch):
        svc.record_list("j1", "hidden", delivery=Delivery(generation_token="garbage"))
    assert _rows(db, "j1") == []


def test_after_an_increment_old_tokens_are_refused(db):
    svc = FeedbackService(db, "config-missing")
    old = _token(db)
    conn = sqlite3.connect(db)
    with conn:
        app_state.increment_generation(conn)
    conn.close()
    with pytest.raises(app_state.GenerationMismatch):
        svc.record_list("j1", "saved", delivery=Delivery(generation_token=old))
    ev = svc.record_list("j1", "saved", delivery=Delivery(generation_token=_token(db)))
    assert ev.generation == 1


def test_unhide_delivered_before_hide_ends_visible(db):
    svc = FeedbackService(db, "config-missing")
    tok = _token(db)
    # the user hid (seq 1) then unhid (seq 2); the network delivers seq 2 first
    svc.record_list("j1", "unhidden", delivery=Delivery("e-unhide-0002", tok, "inst-A", 2))
    late = svc.record_list("j1", "hidden", delivery=Delivery("e-hide-00001", tok, "inst-A", 1))
    assert late.superseded is True
    store = FeedbackStorage(db)
    store.init_db()
    try:
        assert store.list_state(["j1"]) == {"j1": {"saved": False, "hidden": False}}
        assert store.hidden_job_ids() == set()
    finally:
        store.close()


def test_other_instance_or_family_is_not_superseded(db):
    svc = FeedbackService(db, "config-missing")
    tok = _token(db)
    svc.record_list("j1", "saved", delivery=Delivery("e-save-00009", tok, "inst-A", 9))
    # another family on the same offer and instance: independent
    assert svc.record_list("j1", "hidden", delivery=Delivery("e-hide-00003", tok, "inst-A", 3)).superseded is False
    # another browser profile: server insert order decides
    assert svc.record_list("j1", "unsaved", delivery=Delivery("e-unsv-00001", tok, "inst-B", 1)).superseded is False
    store = FeedbackStorage(db)
    store.init_db()
    try:
        assert store.list_state(["j1"]) == {"j1": {"saved": False, "hidden": True}}
    finally:
        store.close()


def test_client_time_trust_window():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    iso = now.isoformat()
    assert client_time_trusted((now - timedelta(days=3)).isoformat(), iso)
    assert client_time_trusted((now + timedelta(minutes=4)).isoformat(), iso)
    assert not client_time_trusted((now + timedelta(minutes=10)).isoformat(), iso)
    assert not client_time_trusted((now - timedelta(days=31)).isoformat(), iso)
    assert not client_time_trusted("not a date", iso)


def test_skewed_client_time_is_stored_untrusted(db):
    svc = FeedbackService(db, "config-missing")
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    ev = svc.record_list("j2", "saved", delivery=Delivery(client_created_at=future))
    assert ev.client_time_trusted is False
    assert _rows(db, "j2")[0]["client_time_trusted"] == 0


def test_hide_reason_is_validated(db):
    from src.feedback.service import FeedbackError

    svc = FeedbackService(db, "config-missing")
    assert svc.record_list("j1", "hidden", ["too_far"]).reasons == ["too_far"]
    with pytest.raises(FeedbackError):
        svc.record_list("j1", "saved", ["too_far"])
    with pytest.raises(FeedbackError):
        svc.record_list("j1", "liked")


def test_journal_stays_append_only(db):
    FeedbackService(db, "config-missing").record_list("j1", "saved")
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE feedback_events SET superseded = 1")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM feedback_events")
    finally:
        conn.close()


def test_api_session_header_and_409(isolated_app):
    with TestClient(isolated_app) as client:
        Storage(db_path=isolated_app.state.db_path).upsert_job(_job("j1"))
        sess = client.get("/api/session").json()
        assert sess["erasure_generation"] == 0 and sess["token"].endswith(":0")
        r = client.get("/api/jobs/feedback/vocabulary")
        assert r.headers["X-Erasure-Generation"] == sess["token"]
        assert "hidden" in r.json()["list_types"]

        body = {"type": "hidden", "reasons": ["too_far"], "source": "ui", "client_event_id": "evt-api-0001",
                "generation": sess["token"], "client_instance_id": "inst-A", "client_seq": 1,
                "client_created_at": datetime.now(UTC).isoformat(),
                "context": {"surface": "jobs", "position": 3, "secret": "dropped"}}
        r = client.post("/api/jobs/j1/feedback", json=body)
        assert r.status_code == 201, r.text
        out = r.json()
        assert out["signal_class"] == "list" and out["generation"] == 0
        assert out["metadata"]["context"] == {"surface": "jobs", "position": 3}
        assert client.post("/api/jobs/j1/feedback", json=body).json()["event_id"] == out["event_id"]
        assert client.get("/api/jobs/feedback/list-state").json() == {"saved": [], "hidden": ["j1"]}

        stale = {**body, "client_event_id": "evt-api-0002", "generation": sess["data_epoch"] + ":7"}
        r = client.post("/api/jobs/j1/feedback", json=stale)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "erasure_generation_mismatch"


def test_legacy_explicit_feedback_without_delivery_still_works(isolated_app):
    with TestClient(isolated_app) as client:
        Storage(db_path=isolated_app.state.db_path).upsert_job(_job("j1"))
        r = client.post("/api/jobs/j1/feedback", json={"type": "interested", "source": "ui"})
        assert r.status_code == 201, r.text
        assert r.json()["generation"] == 0 and r.json()["client_event_id"] is None
