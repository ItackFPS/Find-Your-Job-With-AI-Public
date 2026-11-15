"""Provenance of behaviour events on every list (Phase B B9-A; ADR 0016 event context).

Each page that lists offers sends its own surface and the offer's position; the server keeps the
known context keys, drops the others, keeps explicit / list / implicit events apart (``signal_class``)
and never stores a replayed client event twice. Temporary databases only.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from src.feedback.exposure import ExposureStorage
from src.learning import health as H
from src.models.job import Job
from src.storage.sqlite import Storage


def test_each_list_surface_maps_to_its_stratum() -> None:
    # The Jobs page sends "outside_criteria" for the lane: it is S3, not unknown.
    assert H.surface_code("outside_criteria") == "S3"
    assert H.surface_code("search") == "S2"
    # Recorded with their surface, never counted as evidence of a ranking.
    for surface in ("saved", "review", "job_detail"):
        assert H.surface_code(surface) == "S0", surface


def _seed(db: str) -> None:
    s = Storage(db_path=db)
    s.init_db()
    s.upsert_job(Job(id="j1", source="test", source_job_id="j1", canonical_url="https://example.test/j1",
                     title="Technicien support", company="ACME", description="Poste.", location="Lyon",
                     content_hash="h-j1"))
    s.close()


def _events(db: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT type, source, signal_class, metadata_json FROM feedback_events ORDER BY rowid")]
    finally:
        conn.close()


def test_review_and_detail_answers_keep_their_surface_and_drop_unknown_keys(isolated_app) -> None:
    db = isolated_app.state.db_path
    _seed(db)
    with TestClient(isolated_app) as client:
        r = client.post("/api/jobs/j1/feedback", json={
            "type": "not_interested", "reasons": [], "source": "ui",
            "context": {"surface": "review", "position": 3, "secret": "x"}})
        assert r.status_code == 201, r.text
        r = client.post("/api/jobs/j1/feedback", json={
            "type": "interested", "reasons": [], "source": "ui", "context": {"surface": "job_detail"}})
        assert r.status_code == 201, r.text
        r = client.post("/api/jobs/j1/feedback", json={
            "type": "saved", "reasons": [], "source": "ui",
            "context": {"surface": "decouverte", "position": 1, "randomized": True}})
        assert r.status_code == 201, r.text
    rows = _events(db)
    contexts = [json.loads(r["metadata_json"] or "{}").get("context") for r in rows]
    assert contexts == [{"surface": "review", "position": 3}, {"surface": "job_detail"},
                        {"surface": "decouverte", "position": 1, "randomized": True}]
    # Explicit answers and list actions stay distinguishable in storage.
    assert [r["signal_class"] for r in rows] == ["explicit", "explicit", "list"]
    assert {r["source"] for r in rows} == {"ui"}


def test_a_replayed_client_event_inserts_nothing(isolated_app) -> None:
    db = isolated_app.state.db_path
    _seed(db)
    body = {"type": "saved", "reasons": [], "source": "ui", "client_event_id": "evt-00000001",
            "context": {"surface": "saved", "position": 0}}
    with TestClient(isolated_app) as client:
        token = client.get("/api/session").json()["token"]
        first = client.post("/api/jobs/j1/feedback", json={**body, "generation": token})
        again = client.post("/api/jobs/j1/feedback", json={**body, "generation": token})
    assert first.status_code == 201 and again.status_code == 201
    assert first.json()["event_id"] == again.json()["event_id"]
    assert len(_events(db)) == 1


def test_exposures_from_every_list_are_stored_with_surface_position_and_flag(isolated_app) -> None:
    surfaces = [("jobs", 0, False), ("search", 1, False), ("saved", 2, False), ("decouverte", 0, True),
                ("outside_criteria", 0, False), ("review", 4, False)]
    with TestClient(isolated_app) as client:
        token = client.get("/api/session").json()["token"]
        r = client.post("/api/jobs/feedback/exposure", json={"generation": token, "session_id": "s1", "items": [
            {"job_id": f"j-{s}", "surface": s, "position": p, "randomized": rnd} for s, p, rnd in surfaces]})
        assert r.json() == {"stored": len(surfaces)}
    store = ExposureStorage(isolated_app.state.db_path)
    try:
        rows = {r["surface"]: (r["position"], bool(r["randomized"])) for r in store.rows()}
    finally:
        store.close()
    assert rows == {s: (p, rnd) for s, p, rnd in surfaces}
