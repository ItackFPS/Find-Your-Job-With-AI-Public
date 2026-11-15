"""A journal query on an API request path is stopped after a time limit (P-013, Phase B B1-D)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.decisions.storage import DecisionStorage

SLOW = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"


def test_time_limit_interrupts_a_long_statement_and_is_removed_after(tmp_path: Path) -> None:
    store = DecisionStorage(str(tmp_path / "d.db"))
    store.init_db()
    try:
        t0 = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError) as err, store.time_limit(0.2):
            store._get_conn().execute(SLOW).fetchone()
        assert time.perf_counter() - t0 < 5
        assert store.is_interrupted(err.value)
        # the handler is gone: a normal statement runs to the end
        assert store._get_conn().execute("SELECT COUNT(*) FROM job_decisions").fetchone()[0] == 0
    finally:
        store.close()


def test_no_limit_means_no_handler(tmp_path: Path) -> None:
    store = DecisionStorage(str(tmp_path / "d.db"))
    store.init_db()
    try:
        with store.time_limit(None):
            assert store._get_conn().execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 200000) "
                "SELECT COUNT(*) FROM c").fetchone()[0] == 200000
    finally:
        store.close()


def test_other_errors_are_not_mistaken_for_a_timeout() -> None:
    assert not DecisionStorage.is_interrupted(sqlite3.OperationalError("no such table: x"))
    assert not DecisionStorage.is_interrupted(ValueError("interrupted"))


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from src.api import app as app_module
    from src.api.routes import decisions as routes

    monkeypatch.setattr(routes, "REQUEST_QUERY_SECONDS", 0.2)
    saved = app_module.app.state.db_path
    app_module.app.state.db_path = str(tmp_path / "t.db")
    yield TestClient(app_module.app)
    app_module.app.state.db_path = saved


def _slow(self, *a, **k):
    self._get_conn().execute(SLOW).fetchone()
    raise AssertionError("the slow statement should have been interrupted")


def test_review_queue_answers_503_instead_of_hanging(client, monkeypatch) -> None:
    monkeypatch.setattr(DecisionStorage, "review_queue", _slow)
    t0 = time.perf_counter()
    r = client.get("/api/decisions/review-queue")
    assert r.status_code == 503
    assert "took longer than" in r.json()["detail"]
    assert time.perf_counter() - t0 < 10


def test_run_decisions_answer_503_instead_of_hanging(client, monkeypatch) -> None:
    monkeypatch.setattr(DecisionStorage, "list_for_run", _slow)
    r = client.get("/api/pipeline/runs/r1/decisions")
    assert r.status_code == 503


def test_fast_queries_are_unaffected(client) -> None:
    r = client.get("/api/decisions/review-queue")
    assert r.status_code == 200 and r.json()["total"] == 0
