"""Review queue: latest decisions flagged ``review`` (accepted AND rejected) that the user has not answered."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.decisions.models import DecisionRecord, JobMeta
from src.decisions.storage import DecisionStorage, snapshot_hash, snapshot_payload
from src.models.job import Job
from src.storage.sqlite import Storage


@pytest.fixture
def client(tmp_path: Path):
    from src.api.app import app

    saved = app.state.db_path
    app.state.db_path = str(tmp_path / "t.db")
    yield TestClient(app)
    app.state.db_path = saved


def _rec(job_id: str, outcome: str, at: str, state: str | None, run: str = "r1", snap: str | None = None):
    signals = {"assessment": {"state": state, "reasons": [f"why {job_id}"], "conflicts": []}} if state else {}
    return DecisionRecord(decision_id=f"d-{job_id}-{run}", run_id=run, job_id=job_id, decided_at=at, outcome=outcome,
                          deciding_stage="filter" if outcome == "rejected" else "ranking", strategy_version="sv-1",
                          signals=signals, snapshot_hash=snap, job=JobMeta(title=f"T {job_id}"))


@pytest.fixture
def seeded(client):
    from src.api.app import app

    db = app.state.db_path
    s = Storage(db_path=db)
    s.init_db()
    s.upsert_jobs_batch([Job(id="acc", title="T acc", description="x")])
    s.close()
    job = Job(id="rej", source="s", title="T rej", description="python " * 60)
    payload = snapshot_payload(job)
    h = snapshot_hash(payload)
    st = DecisionStorage(db)
    st.init_db()
    st.write_run([
        _rec("acc", "accepted", "2026-10-01T00:00:00+00:00", "review"),
        _rec("rej", "rejected", "2026-10-01T00:00:00+00:00", "review", snap=h),
        _rec("ok", "accepted", "2026-10-01T00:00:00+00:00", "accept"),
        _rec("old", "rejected", "2026-10-01T00:00:00+00:00", "review"),
    ], {h: {"payload": payload, "job_id": "rej", "meta": JobMeta(title="T rej")}})
    # a newer run no longer flags `old`: only the LATEST decision counts
    st.write_run([_rec("old", "rejected", "2026-10-02T00:00:00+00:00", "reject", run="r2")], {})
    st.close()


def test_queue_lists_latest_review_decisions_including_rejections(client, seeded):
    body = client.get("/api/decisions/review-queue").json()
    assert body["total"] == 2
    items = {i["decision"]["job_id"]: i for i in body["items"]}
    assert set(items) == {"acc", "rej"}
    assert items["acc"]["job_stored"] and not items["acc"]["has_snapshot"]
    assert not items["rej"]["job_stored"] and items["rej"]["has_snapshot"]
    assert items["rej"]["decision"]["signals"]["assessment"]["reasons"] == ["why rej"]
    only_rejected = client.get("/api/decisions/review-queue", params={"outcome": "rejected"}).json()
    assert [i["decision"]["job_id"] for i in only_rejected["items"]] == ["rej"]


def test_feedback_on_a_rejected_job_known_only_to_the_journal_is_accepted_and_resolves_it(client, seeded):
    resp = client.post("/api/jobs/rej/feedback", json={"type": "interested", "source": "ui",
                                                        "decision_id": "d-rej-r1"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["decision_id"] == "d-rej-r1"
    assert [i["decision"]["job_id"] for i in client.get("/api/decisions/review-queue").json()["items"]] == ["acc"]
    assert client.get("/api/jobs/rej/feedback").status_code == 200


def test_feedback_on_an_unknown_job_is_still_404(client, seeded):
    assert client.post("/api/jobs/nope/feedback", json={"type": "interested"}).status_code == 404


def test_empty_database_has_an_empty_queue(client):
    assert client.get("/api/decisions/review-queue").json() == {"total": 0, "limit": 50, "offset": 0, "items": []}
