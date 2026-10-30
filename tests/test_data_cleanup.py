"""Issue #86, slice 3: Settings › Data removes old expired offers, then compacts.

* Only offers expired for longer than the chosen age go, never one the user acted on
  (feedback, an outcome event, an application status); their derived rows go with them.
* The summary counts what each age choice would remove.
* The API runs the cleanup in the background, once at a time, and refuses while a
  search runs.

Temporary databases only.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from src.storage.cleanup import data_summary, remove_expired
from src.storage.sqlite import Storage

NOW = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


def _seed(db: Path) -> None:
    Storage._initialized_paths.discard(str(db.resolve()))
    s = Storage(db_path=str(db))
    s.init_db()
    jobs = [Job(id=i, source="france-travail", title=f"Offre {i}", sources=["france-travail"])
            for i in ("old", "recent", "liked", "applied", "outcome", "active")]
    for job in jobs:
        s.upsert_job(job)
    s.record_provenance(jobs, NOW - timedelta(days=400))
    s.close()
    conn = sqlite3.connect(str(db))
    old, recent = (NOW - timedelta(days=200)).isoformat(), (NOW - timedelta(days=10)).isoformat()
    for job_id, when in (("old", old), ("recent", recent), ("liked", old), ("applied", old), ("outcome", old)):
        conn.execute("UPDATE jobs SET status = 'expired', expired_at = ?, expiry_reason = 'not_seen' WHERE id = ?",
                     (when, job_id))
    conn.execute("UPDATE jobs SET application_status = 'applied' WHERE id = 'applied'")
    conn.execute("CREATE TABLE IF NOT EXISTS job_feedback (id TEXT PRIMARY KEY, job_id TEXT NOT NULL)")
    conn.execute("INSERT INTO job_feedback VALUES ('f1', 'liked')")
    conn.execute("CREATE TABLE IF NOT EXISTS outcome_events (id TEXT PRIMARY KEY, job_id TEXT NOT NULL)")
    conn.execute("INSERT INTO outcome_events VALUES ('o1', 'outcome')")
    conn.commit()
    conn.close()


def _ids(db: Path, table: str = "jobs", column: str = "id") -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute(f"SELECT {column} FROM {table}")}
    finally:
        conn.close()


def test_only_old_expired_offers_nobody_acted_on_are_removed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _seed(db)
    result = remove_expired(db, older_than_days=90, now=NOW)
    assert result["removed"] == 1
    assert _ids(db) == {"recent", "liked", "applied", "outcome", "active"}
    assert "old" not in _ids(db, "job_provenance", "job_id")
    assert "old" not in _ids(db, "job_fts", "id")
    assert result["bytes_after"] > 0


def test_the_summary_counts_what_each_age_would_remove(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _seed(db)
    summary = data_summary(db, now=NOW)
    assert summary["offers"] == {"active": 1, "expired": 5}
    assert summary["removable_expired"] == {"30": 1, "90": 1, "180": 1}
    assert summary["database_bytes"] > 0
    with pytest.raises(ValueError):
        remove_expired(db, older_than_days=0, now=NOW)


class _Runner:
    def __init__(self, running: bool) -> None:
        self.running = running

    def is_running(self) -> bool:
        return self.running


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from src.api.app import app

    db = tmp_path / "jobs.db"
    _seed(db)
    for key, value in {"db_path": str(db), "runner": _Runner(False), "scheduler": None,
                       "linkedin_scanner": None, "hellowork_scanner": None}.items():
        monkeypatch.setitem(app.state._state, key, value)
    return TestClient(app)


def test_the_cleanup_runs_in_the_background(client) -> None:
    body = client.get("/api/settings/data").json()
    assert body["offers"]["expired"] == 5
    assert body["cleanup"]["state"] in ("idle", "done", "failed")

    r = client.post("/api/settings/data/cleanup", json={"older_than_days": 30})
    assert r.status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        state = client.get("/api/settings/data").json()["cleanup"]
        if state["state"] != "running":
            break
        time.sleep(0.05)
    assert state["state"] == "done"
    assert state["removed"] == 1                    # "old" only: "recent" expired 10 days ago
    assert client.get("/api/settings/data").json()["offers"]["expired"] == 4


def test_the_cleanup_waits_for_a_running_search(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(client.app.state._state, "runner", _Runner(True))
    r = client.post("/api/settings/data/cleanup", json={"older_than_days": 30})
    assert (r.status_code, r.json()["detail"]) == (409, "search_running")
    assert client.post("/api/settings/data/cleanup", json={"older_than_days": 1}).status_code == 422
