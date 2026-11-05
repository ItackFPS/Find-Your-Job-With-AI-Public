"""Visible exposure batches and Hide in job lists (ADR 0015, ADR 0016, ADR 0017)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from src.feedback.exposure import MAX_BATCH, ExposureItem, ExposureStorage
from src.models.job import Job
from src.storage import app_state
from src.storage.sqlite import Storage


def _job(job_id: str, content_hash: str = "") -> Job:
    return Job(id=job_id, source="test", source_job_id=job_id, canonical_url=f"https://example.test/{job_id}",
               title=f"Développeur {job_id}", company="ACME", description="Poste de développeur.",
               location="Lyon", content_hash=content_hash or f"hash-{job_id}")


def _store(tmp_path) -> tuple[str, ExposureStorage]:
    db = str(tmp_path / "jobs.db")
    store = ExposureStorage(db)
    store.init_db()
    return db, store


def test_batch_with_current_token_is_stored(tmp_path):
    db, store = _store(tmp_path)
    try:
        n = store.record_batch(app_state.current(db).token, "sess-1", [
            ExposureItem("j1", "jobs", position=0, visible_ms=1400), ExposureItem("j2", "jobs", position=1)])
        assert n == 2
        rows = store.rows()
        assert [r["job_id"] for r in rows] == ["j1", "j2"]
        assert rows[0]["generation"] == 0 and rows[0]["session_id"] == "sess-1"
    finally:
        store.close()


def test_missing_or_stale_token_drops_the_batch(tmp_path):
    db, store = _store(tmp_path)
    try:
        epoch = app_state.current(db).data_epoch
        item = [ExposureItem("j1", "jobs")]
        assert store.record_batch(None, None, item) == 0
        assert store.record_batch(f"{epoch}:3", None, item) == 0
        assert store.record_batch("garbage", None, item) == 0
        assert store.count() == 0
    finally:
        store.close()


def test_batch_is_capped(tmp_path):
    db, store = _store(tmp_path)
    try:
        items = [ExposureItem(f"j{i}", "jobs") for i in range(MAX_BATCH + 5)]
        assert store.record_batch(app_state.current(db).token, None, items) == MAX_BATCH
        assert store.count() == MAX_BATCH
    finally:
        store.close()


def test_retention_compacts_after_30_days_and_deletes_after_180(tmp_path):
    db, store = _store(tmp_path)
    tok = app_state.current(db).token
    now = datetime(2026, 11, 5, tzinfo=UTC)
    try:
        store.record_batch(tok, None, [ExposureItem("old", "jobs", position=4, visible_ms=900)],
                           now=now - timedelta(days=200))
        store.record_batch(tok, None, [ExposureItem("mid", "jobs", position=5, visible_ms=1000)],
                           now=now - timedelta(days=40))
        store.record_batch(tok, None, [ExposureItem("mid", "jobs", position=2, visible_ms=3000)],
                           now=now - timedelta(days=35))
        store.record_batch(tok, None, [ExposureItem("new", "jobs", position=1, visible_ms=1200)],
                           now=now - timedelta(days=2))
        assert store.apply_retention(now=now) == {"deleted": 1, "compacted": 2}
        rows = {r["job_id"]: r for r in store.rows()}
        assert set(rows) == {"mid", "new"}
        assert rows["mid"]["compacted"] == 1 and rows["mid"]["position"] == 2
        assert rows["mid"]["visible_ms"] is None
        assert rows["mid"]["occurred_at"] == (now - timedelta(days=40)).isoformat(timespec="seconds")
        assert rows["new"]["visible_ms"] == 1200
        # idempotent
        assert store.apply_retention(now=now) == {"deleted": 0, "compacted": 0}
    finally:
        store.close()


def _post_list(client, job_id: str, type_: str) -> None:
    r = client.post(f"/api/jobs/{job_id}/feedback", json={"type": type_, "source": "ui"})
    assert r.status_code == 201, r.text


def test_hidden_offer_and_its_exact_duplicates_leave_the_list(isolated_app):
    with TestClient(isolated_app) as client:
        s = Storage(db_path=isolated_app.state.db_path)
        for j in (_job("a"), _job("b", "same"), _job("c", "same"), _job("d")):
            s.upsert_job(j)
        s.close()
        _post_list(client, "b", "hidden")
        ids = {j["id"] for j in client.get("/api/jobs").json()}
        assert ids == {"a", "d"}
        listed = {j["id"]: j for j in client.get("/api/jobs?include_hidden=true").json()}
        assert set(listed) == {"a", "b", "c", "d"}
        assert listed["b"]["hidden"] is True and listed["c"]["hidden"] is False
        # search obeys the same rule
        assert "b" not in {j["id"] for j in client.get("/api/jobs?q=Développeur").json()}
        _post_list(client, "b", "unhidden")
        assert {j["id"] for j in client.get("/api/jobs").json()} == {"a", "b", "c", "d"}


def test_saved_only_and_detail_flags(isolated_app):
    with TestClient(isolated_app) as client:
        s = Storage(db_path=isolated_app.state.db_path)
        for j in (_job("a"), _job("b"), _job("c")):
            s.upsert_job(j)
        s.close()
        _post_list(client, "a", "saved")
        _post_list(client, "c", "saved")
        _post_list(client, "c", "unsaved")
        saved = client.get("/api/jobs?saved_only=true").json()
        assert [j["id"] for j in saved] == ["a"] and saved[0]["saved"] is True
        assert client.get("/api/jobs/a").json()["saved"] is True
        assert client.get("/api/jobs/b").json()["saved"] is False


def test_exposure_endpoint(isolated_app):
    with TestClient(isolated_app) as client:
        tok = client.get("/api/session").json()["token"]
        body = {"generation": tok, "session_id": "s1",
                "items": [{"job_id": "a", "surface": "jobs", "position": 0, "visible_ms": 1100}]}
        assert client.post("/api/jobs/feedback/exposure", json=body).json() == {"stored": 1}
        assert client.post("/api/jobs/feedback/exposure", json={**body, "generation": None}).json() == {"stored": 0}
        too_many = {**body, "items": body["items"] * (MAX_BATCH + 1)}
        assert client.post("/api/jobs/feedback/exposure", json=too_many).status_code == 422
