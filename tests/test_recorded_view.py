"""What was recorded (Phase B B9-A): a read-only summary of the behaviour data kept on this computer,
and Erase removes every field it shows. Temporary databases only."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from src.feedback.exposure import ExposureItem, ExposureStorage
from src.feedback.service import Delivery, FeedbackService
from src.models.job import Job
from src.privacy import erasure, learning
from src.privacy.recorded import summary
from src.storage import app_state
from src.storage.sqlite import Storage


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "jobs.db")
    s = Storage(db_path=db)
    s.init_db()
    for jid, source in (("j1", "eures"), ("j2", "indeed")):
        s.upsert_job(Job(id=jid, source=source, source_job_id=jid, canonical_url=f"https://example.test/{jid}",
                         title="Technicien support", company="ACME", description="Poste.", location="Lyon",
                         content_hash=hashlib.sha1(jid.encode()).hexdigest()))
    s.close()
    return db


def _record(db: str, cfg: Path) -> None:
    svc = FeedbackService(db, config_dir=str(cfg))
    svc.record_explicit("j1", "not_interested", source="ui",
                        delivery=Delivery(context={"surface": "review", "position": 2}))
    svc.record_list("j2", "saved", delivery=Delivery(context={"surface": "saved", "position": 0}))
    svc.record_behavior("j1", "viewed")
    svc.record_behavior("j1", "viewed")          # a repeated view is recorded too
    store = ExposureStorage(db)
    store.init_db()
    try:
        store.record_batch(app_state.current(db).token, "s1", [
            ExposureItem("j1", "review", position=2), ExposureItem("j2", "jobs", position=0),
            ExposureItem("j2", "decouverte", position=0, randomized=True)])
    finally:
        store.close()


def test_an_empty_install_has_nothing_recorded(tmp_path: Path) -> None:
    out = summary(tmp_path / "missing.db")
    assert out["events"] == [] and out["exposures"]["total"] == 0
    assert out["exposures"]["compact_after_days"] == 30 and out["exposures"]["keep_days"] == 180
    assert not (tmp_path / "missing.db").exists()         # reading never creates the database


def test_summary_counts_events_surfaces_and_exposures(tmp_path: Path) -> None:
    db = _db(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    _record(db, cfg)
    out = summary(db)
    by_type = {e["type"]: e for e in out["events"]}
    assert by_type["not_interested"]["signal_class"] == "explicit" and by_type["not_interested"]["count"] == 1
    assert by_type["saved"]["signal_class"] == "list"
    assert by_type["viewed"]["signal_class"] == "behavior" and by_type["viewed"]["count"] == 2
    assert out["event_surfaces"]["review"] == 1 and out["event_surfaces"]["saved"] == 1
    assert out["exposures"]["total"] == 3
    assert out["exposures"]["by_surface"] == {"decouverte": 1, "jobs": 1, "review": 1}
    assert out["exposures"]["by_source"] == {"eures": 1, "indeed": 2}
    assert out["repeated_views"] == "counted once per offer"


def test_events_before_a_reset_are_shown_but_not_counted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    _record(db, cfg)
    learning.reset(db)
    out = summary(db)
    assert out["learning"]["reset_at"]
    assert all(e["count"] >= 1 and e["counted"] == 0 for e in out["events"])


def test_erase_removes_every_field_the_view_shows(tmp_path: Path) -> None:
    db = _db(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    _record(db, cfg)
    erasure.request(db, erasure.Scope(keep_tracker=False, erase_calibration=True))
    erasure.run(db)
    out = summary(db)
    assert out["events"] == [] and out["event_surfaces"] == {}
    assert out["exposures"]["total"] == 0 and out["exposures"]["by_surface"] == {}
    assert out["exposures"]["by_source"] == {}


def test_api_returns_the_summary(isolated_app) -> None:
    with TestClient(isolated_app) as client:
        r = client.get("/api/privacy/recorded")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"learning", "events", "event_surfaces", "exposures", "repeated_views"}
