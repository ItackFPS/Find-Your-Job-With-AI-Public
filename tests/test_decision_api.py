"""Read-only decision journal API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.decisions.models import (
    ConflictItem, DecisionRecord, EvidenceItem, Exposure, JobMeta, SourceRef, SpanRef, StageTraceEntry,
)
from src.decisions.storage import DecisionStorage, snapshot_hash, snapshot_payload
from src.models.job import Job

NOW = datetime.now(UTC).isoformat(timespec="seconds")


def _ev(polarity="positive", state="satisfied", dim="skills", span=False):
    return EvidenceItem(dimension=dim, polarity=polarity, state=state, source=SourceRef(stage="filter", rule_id="r"),
                        message="m", span=SpanRef(start=0, end=3, text="abc") if span else None)


@pytest.fixture
def client(tmp_path: Path):
    from src.api.app import app

    saved = app.state.db_path
    app.state.db_path = str(tmp_path / "t.db")
    yield TestClient(app)
    app.state.db_path = saved


@pytest.fixture
def seeded(client):
    from src.api.app import app

    job = Job(id="rej1", source="s", title="Ruby dev", description="ruby " * 80)
    payload = snapshot_payload(job)
    h = snapshot_hash(payload)
    recs = [
        DecisionRecord(decision_id="d-acc", run_id="run1", job_id="acc1", decided_at=NOW, outcome="accepted",
                       deciding_stage="ranking", strategy_version="sv-1", fit_score=0.83,
                       evidence=[_ev(), _ev("negative", "violated"), _ev("unknown", "unknown")],
                       conflicts=[ConflictItem(description="x")], exposure=Exposure(rank=1, tier="excellent"),
                       stage_trace=[StageTraceEntry(stage="score", result="pass")], job=JobMeta(title="A")),
        DecisionRecord(decision_id="d-rej", run_id="run1", job_id="rej1", decided_at=NOW, outcome="rejected",
                       deciding_stage="requirements_gate", strategy_version="sv-1", evidence=[_ev("negative", "violated", span=True)],
                       snapshot_hash=h, job=JobMeta(title="Ruby dev")),
        DecisionRecord(decision_id="d-rej2", run_id="run1", job_id="rej2", decided_at=NOW, outcome="rejected",
                       deciding_stage="filter", strategy_version="sv-1", job=JobMeta(title="B")),
    ]
    st = DecisionStorage(app.state.db_path)
    st.init_db()
    st.register_strategy({"strategy_version": "sv-1", "fingerprint": "f", "files": {"profile": "h"}, "extra": {}})
    st.write_run(recs, {h: {"payload": payload, "job_id": "rej1", "meta": JobMeta(title="Ruby dev")}})
    st.close()
    return recs


def test_job_decision_answers_the_audit_questions(client, seeded):
    body = client.get("/api/jobs/acc1/decision").json()
    assert body["outcome"] == "accepted" and body["deciding_stage"] == "ranking" and body["strategy_version"] == "sv-1"
    assert body["fit_score"] == 0.83 and body["exposure"]["rank"] == 1
    assert body["summary"] == {
        "supporting": 1, "against": 1, "unknown": 1, "conflicts": 1,
        "headline": "accepted at ranking (score 0.83): 1 supporting, 1 against, 1 unknown, 1 conflict(s) — strategy sv-1"}
    assert [e["state"] for e in body["unknown_evidence"]] == ["unknown"]


def test_rejected_job_decision_is_available_even_though_the_job_row_is_not(client, seeded):
    body = client.get("/api/jobs/rej1/decision").json()      # rejected jobs are never in the `jobs` table
    assert body["outcome"] == "rejected" and body["fit_score"] is None
    assert client.get("/api/jobs/nope/decision").status_code == 404


def test_decision_by_id_includes_the_strategy_manifest(client, seeded):
    body = client.get("/api/decisions/d-acc").json()
    assert body["strategy"] == {"strategy_version": "sv-1", "manifest": {
        "strategy_version": "sv-1", "fingerprint": "f", "files": {"profile": "h"}, "extra": {}}}
    assert client.get("/api/decisions/zzz").status_code == 404


def test_snapshot_is_served_only_when_text_was_kept(client, seeded):
    assert client.get("/api/decisions/d-rej/snapshot").json()["title"] == "Ruby dev"
    assert client.get("/api/decisions/d-acc/snapshot").status_code == 404           # never stored
    assert client.get("/api/decisions/d-rej2/snapshot").status_code == 404
    assert client.get("/api/decisions/zzz/snapshot").status_code == 404


def test_purged_snapshot_is_410_not_a_silent_404(client, seeded):
    import sqlite3

    from src.api.app import app

    conn = sqlite3.connect(app.state.db_path)
    conn.execute("UPDATE job_snapshots SET last_seen_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    st = DecisionStorage(app.state.db_path)
    st.init_db()
    assert st.purge_text(60)[0] == 1
    st.close()
    assert client.get("/api/decisions/d-rej/snapshot").status_code == 410
    assert client.get("/api/jobs/rej1/decision").status_code == 200                # the audit trail survives


def test_run_listing_filters_and_paginates(client, seeded):
    page = client.get("/api/pipeline/runs/run1/decisions").json()
    assert page["total"] == 3 and len(page["decisions"]) == 3
    rej = client.get("/api/pipeline/runs/run1/decisions?outcome=rejected").json()
    assert rej["total"] == 2 and {d["job_id"] for d in rej["decisions"]} == {"rej1", "rej2"}
    gate = client.get("/api/pipeline/runs/run1/decisions?stage=requirements_gate").json()
    assert [d["job_id"] for d in gate["decisions"]] == ["rej1"]
    two = client.get("/api/pipeline/runs/run1/decisions?limit=2&offset=2").json()
    assert two["total"] == 3 and len(two["decisions"]) == 1
    assert client.get("/api/pipeline/runs/run1/decisions?outcome=bogus").status_code == 422
    assert client.get("/api/pipeline/runs/run1/decisions?limit=0").status_code == 422
    assert client.get("/api/pipeline/runs/none/decisions").json()["total"] == 0


def test_api_is_read_only(client, seeded):
    for path in ("/api/jobs/acc1/decision", "/api/decisions/d-acc", "/api/pipeline/runs/run1/decisions"):
        assert client.post(path).status_code == 405 and client.delete(path).status_code == 405
