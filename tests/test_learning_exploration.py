"""Découverte (S4): a randomized draw independent of both rankings (ADR 0019; stage L4.3)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.feedback.exposure import ExposureStorage
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage
from src.learning import exploration as X

NOW = datetime(2026, 9, 15, tzinfo=UTC)
IDS = [f"j{i:02d}" for i in range(20)]


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "jobs.db")
    fb = FeedbackStorage(db_path=path)
    fb.init_db()
    ex = ExposureStorage(db_path=path)
    ex.init_db()
    yield path, fb
    fb.close()
    ex.close()


def test_draw_is_seeded_uniform_and_recorded(db):
    path, _ = db
    d = X.draw(path, "r1", IDS, now=NOW, seed="abc")
    assert d.pool_size == 20 and len(d.drawn) == 2 and d.pi == pytest.approx(0.1)
    assert X.draw(path, "r2", list(reversed(IDS)), now=NOW, seed="abc").drawn == d.drawn   # input order is irrelevant
    rows = X.drawn_for_run(path, "r1")
    assert [(r["job_id"], r["slot"], r["pi"], r["seed"], r["pool_size"]) for r in rows] == [
        (d.drawn[0], 1, 0.1, "abc", 20), (d.drawn[1], 2, 0.1, "abc", 20)]


def test_draw_never_sees_a_score():
    """The draw takes ids only: any scores (any permutation of them) leave it unchanged by construction."""
    import inspect

    assert list(inspect.signature(X.draw).parameters)[:3] == ["db_path", "run_id", "candidate_ids"]


def test_every_eligible_offer_can_be_drawn(db):
    path, _ = db
    seen = set()
    for i in range(200):
        seen |= set(X.draw(path, f"r{i}", IDS[:5], now=NOW, seed=f"s{i}").drawn)
    assert seen == set(IDS[:5])


def test_hidden_and_recently_shown_offers_are_excluded(db):
    path, fb = db
    fb.append(FeedbackEvent(event_id="h", job_id="j00", signal_class="list", type="hidden", reasons=[],
                            source="ui", occurred_at=NOW.isoformat(timespec="seconds"), metadata={}))
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO exposure_log (job_id, surface, position, session_id, generation, occurred_at) "
                     "VALUES (?, ?, 1, 's', 0, ?)",
                     [("j01", "decouverte", (NOW - timedelta(days=3)).isoformat()),     # shown in S4: excluded
                      ("j02", "decouverte", (NOW - timedelta(days=20)).isoformat()),    # outside the window
                      ("j03", "jobs", (NOW - timedelta(days=1)).isoformat())])          # other surface
    conn.commit()
    conn.close()
    d = X.draw(path, "r", IDS[:4], now=NOW, seed="x", slots=2)
    assert d.pool_size == 2 and set(d.drawn) == {"j02", "j03"}


def test_small_and_empty_pools(db):
    path, _ = db
    assert X.draw(path, "r1", ["only"], now=NOW).drawn == ("only",)
    empty = X.draw(path, "r2", [], now=NOW)
    assert empty.drawn == () and empty.pool_size == 0 and empty.pi == 0.0
    assert X.drawn_for_run(path, "r2") == []
    assert X.draw(path, "r3", IDS, now=NOW, slots=0).drawn == ()


def test_slots_config(tmp_path):
    assert X.load_slots(Path("config/personalization.yaml")) == 2
    assert X.load_slots(tmp_path / "missing.yaml") == 2
    for raw, want in [("5", 2), ("1", 1), ("0", 0), ("-1", 0), ("true", 2), ("x", 2)]:
        p = tmp_path / f"c{raw}.yaml"
        p.write_text(f"personal_model:\n  exploration:\n    slots: {raw}\n", encoding="utf-8")
        assert X.load_slots(p) == want, raw


def test_drawn_for_run_without_table(tmp_path):
    assert X.drawn_for_run(str(tmp_path / "none.db"), "r") == []


def test_record_in_shadow(db):
    from src.learning.shadow import ensure_schema

    path, _ = db
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    conn.executemany("INSERT INTO ranking_shadow (run_id, job_id, production_position, challenger_position, "
                     "personal_adjustment, model_version) VALUES ('r', ?, ?, ?, 0.0, 'v')",
                     [(j, i, i) for i, j in enumerate(IDS[:4], start=1)])
    conn.commit()
    conn.close()
    d = X.draw(path, "r", IDS[:4], now=NOW, seed="z")
    X.record_in_shadow(path, d)
    conn = sqlite3.connect(path)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT job_id, s4_seed, s4_pool_size, s4_pi FROM ranking_shadow")}
    conn.close()
    assert all(r[:2] == ("z", 4) for r in rows.values())
    assert {j for j, r in rows.items() if r[2] is not None} == set(d.drawn)
    assert {r[2] for j, r in rows.items() if j in d.drawn} == {0.5}


# -- pipeline and API ---------------------------------------------------------------------------

def test_pipeline_draws_from_scored_offers(tmp_path):
    from tests.test_decision_journal_orchestrator import _config_dir
    from tests.test_learning_model_shadow import _pipeline

    cfg = _config_dir(tmp_path, None)
    db = tmp_path / "jobs.db"
    result = _pipeline(db, cfg)
    drawn = X.drawn_for_run(str(db), "run")
    assert len(drawn) == min(2, len(result.scored_jobs))
    assert {d["job_id"] for d in drawn} <= {s.job_id for s in result.scored_jobs}   # positions still computed
    conn = sqlite3.connect(db)
    pis = conn.execute("SELECT COUNT(*) FROM ranking_shadow WHERE run_id = 'run' AND s4_pi IS NOT NULL").fetchone()[0]
    conn.close()
    assert pis == len(drawn)


def test_pipeline_draw_fails_open(tmp_path):
    from unittest.mock import patch

    from tests.test_decision_journal_orchestrator import _config_dir
    from tests.test_learning_model_shadow import _pipeline

    with patch("src.learning.exploration.draw", side_effect=RuntimeError("boom")):
        result = _pipeline(tmp_path / "jobs.db", _config_dir(tmp_path, None))
    assert result.scored_jobs and X.drawn_for_run(str(tmp_path / "jobs.db"), "run") == []


def test_api_serves_decouverte_apart_from_the_main_list(isolated_app):
    from fastapi.testclient import TestClient

    from src.models.job import Job
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db = isolated_app.state.db_path
    with TestClient(isolated_app) as client:
        s = Storage(db_path=db)
        for j in "abcd":
            s.upsert_job(Job(id=j, source="test", source_job_id=j, canonical_url=f"https://example.test/{j}",
                             title=f"Développeur {j}", company="ACME", description="Poste.", location="Lyon",
                             content_hash=f"hash-{j}"))
        s.close()
        rs = RunStorage(db_path=db)
        rs.init_db()
        rs.create_run("r1")
        rs.store_job_scores("r1", [{"job_id": j, "final_score": sc, "score_breakdown": {}}
                                   for j, sc in [("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6)]])
        rs.complete_run("r1", {})
        rs.close()
        assert client.get("/api/jobs/decouverte").json() == []
        X.draw(db, "r1", ["a", "b", "c", "d"], seed="fixed", slots=2)
        drawn = [d["job_id"] for d in X.drawn_for_run(db, "r1")]
        got = client.get("/api/jobs/decouverte").json()
        assert [j["id"] for j in got] == drawn
        main = [j["id"] for j in client.get("/api/jobs").json()]
        assert set(main) == set("abcd") - set(drawn)                    # S1 never duplicates S4
        assert set(drawn) <= {j["id"] for j in client.get("/api/jobs?q=Développeur").json()}   # search keeps them
        r = client.post(f"/api/jobs/{drawn[0]}/feedback", json={"type": "hidden", "source": "ui"})
        assert r.status_code < 300
        assert [j["id"] for j in client.get("/api/jobs/decouverte").json()] == drawn[1:]
