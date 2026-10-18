"""Storage, retention, configuration and contracts of the decision journal."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.decisions.config import DecisionConfig, load_decision_config
from src.decisions.models import (
    DecisionRecord, EvidenceItem, Exposure, JobMeta, SourceRef, StageTraceEntry,
)
from src.decisions.storage import DecisionStorage, snapshot_hash, snapshot_payload
from src.models.job import Job


_NOW = datetime.now(UTC).isoformat(timespec="seconds")


def _record(job_id="j1", run_id="r1", snap=None, decided_at=_NOW, outcome="rejected", decision_id=None, span=False):
    from src.decisions.models import SpanRef

    return DecisionRecord(
        decision_id=decision_id or f"d-{run_id}-{job_id}", run_id=run_id, job_id=job_id, decided_at=decided_at, outcome=outcome,
        deciding_stage="filter", strategy_version="sv-abc",
        evidence=[
            EvidenceItem(dimension="location", polarity="negative", state="violated",
                         source=SourceRef(stage="filter", rule_id="filter.location"), message="too far",
                         span=SpanRef(start=0, end=9, text="Bac+5 req.") if span else None),
            EvidenceItem(dimension="experience", polarity="unknown", state="unknown",
                         source=SourceRef(stage="requirements_gate", rule_id="gate.flag"), message="years unclear"),
        ],
        stage_trace=[StageTraceEntry(stage="filter", result="reject", detail="location")],
        exposure=Exposure(rank=3, tier="strong") if outcome == "accepted" else None,
        snapshot_hash=snap, job=JobMeta(title="Dev", company="Acme", source="s", location="Paris"),
    )


@pytest.fixture()
def store(tmp_path):
    s = DecisionStorage(str(tmp_path / "jobs.db"))
    s.init_db()
    s.init_db()                                  # idempotent
    yield s
    s.close()


def _job(desc="Description python " * 20):
    return Job(id="j1", source="s", title="Dev", company="Acme", description=desc, location="Paris", skills=["python"])


def test_roundtrip_preserves_everything(store):
    job = _job()
    payload = snapshot_payload(job)
    h = snapshot_hash(payload)
    n, new = store.write_run([_record(snap=h)], {h: {"payload": payload, "job_id": "j1", "meta": JobMeta(title="Dev")}})
    assert (n, new) == (1, 1)
    got = store.get_latest_decision("j1")
    assert got == _record(snap=h)
    assert [e.message for e in got.unknowns] == ["years unclear"]
    assert store.get_decision("d-r1-j1") == got
    assert store.get_snapshot(h)["description"] == job.description


def test_one_decision_per_run_and_job(store):
    """Production decision ids are fresh uuids: only the (run_id, job_id) unique index enforces this."""
    assert store.write_run([_record(decision_id="a")], {})[0] == 1
    assert store.write_run([_record(decision_id="b")], {})[0] == 0         # same (run, job): ignored
    assert store.write_run([_record(run_id="r2", decision_id="c")], {})[0] == 1


def test_other_constraint_violations_are_not_silently_ignored(store):
    bad = _record(decision_id="x", run_id="r9")
    bad = bad.model_copy(update={"strategy_version": None})                  # NOT NULL column
    with pytest.raises(sqlite3.IntegrityError):
        store.write_run([bad], {})


def test_snapshot_is_content_addressed_and_text_optionally_uncompressed(store):
    payload = snapshot_payload(_job())
    h = snapshot_hash(payload)
    assert h == snapshot_hash(snapshot_payload(_job()))                    # same content → same hash
    assert h != snapshot_hash(snapshot_payload(_job("different text " * 20)))
    for compress in (True, False):
        store.write_run([], {h: {"payload": payload, "job_id": "j1", "meta": JobMeta()}}, compress=compress)
        assert store.get_snapshot(h)["title"] == "Dev"
    assert store.write_run([], {h: {"payload": payload, "job_id": "j1", "meta": JobMeta()}})[1] == 0


def test_purge_text_keeps_decision_and_metadata(store):
    payload = snapshot_payload(_job())
    h = snapshot_hash(payload)
    store.write_run([_record(snap=h)], {h: {"payload": payload, "job_id": "j1", "meta": JobMeta(title="Dev", company="Acme")}})
    conn = sqlite3.connect(store._db_path)
    conn.execute("UPDATE job_snapshots SET last_seen_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert store.purge_text(0) == (0, 0)                                   # 0 = keep forever
    assert store.purge_text(60) == (1, 0)
    assert store.purge_text(60) == (0, 0)                                  # idempotent
    assert store.get_snapshot(h) is None                                   # text gone…
    rec = store.get_latest_decision("j1")
    assert rec and rec.snapshot_hash == h and rec.job.title == "Dev"       # …audit trail stays
    row = conn.execute("SELECT title, company, text_purged_at FROM job_snapshots").fetchone()
    assert row[0] == "Dev" and row[1] == "Acme" and row[2]


def test_purge_records_removes_old_decisions_and_orphan_snapshots(store):
    p1, p2 = snapshot_payload(_job("a " * 100)), snapshot_payload(_job("b " * 100))
    h1, h2 = snapshot_hash(p1), snapshot_hash(p2)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")
    store.write_run(
        [_record("old", decided_at=old, snap=h1), _record("new", run_id="r2", snap=h2)],
        {h1: {"payload": p1, "job_id": "old", "meta": JobMeta()}, h2: {"payload": p2, "job_id": "new", "meta": JobMeta()}},
    )
    assert store.purge_records(0) == (0, 0)
    assert store.purge_records(365) == (1, 1)
    assert store.get_latest_decision("old") is None and store.get_latest_decision("new") is not None
    assert store.get_snapshot(h1) is None and store.get_snapshot(h2) is not None


def test_strategy_registry(store):
    m = {"strategy_version": "sv-1", "fingerprint": "f", "files": {"profile": "h"}, "extra": {}}
    store.register_strategy(m)
    store.register_strategy({**m, "files": {"profile": "OTHER"}})          # first registration wins
    assert store.get_strategy("sv-1") == m and store.get_strategy("nope") is None


def test_config_defaults_and_resilience(tmp_path):
    assert load_decision_config(tmp_path) == DecisionConfig()
    (tmp_path / "decisions.yaml").write_text("enabled: false\ntext_retention_days: 7\n")
    c = load_decision_config(tmp_path)
    assert c.enabled is False and c.text_retention_days == 7
    for bad in ("enabled: [1\n", "nonsense_key: 1\n", "text_retention_days: -3\n"):
        (tmp_path / "decisions.yaml").write_text(bad)
        assert load_decision_config(tmp_path) == DecisionConfig()           # never blocks the pipeline


def test_contracts_are_strict():
    with pytest.raises(ValidationError):
        EvidenceItem(dimension="nope", polarity="negative", state="violated", source=SourceRef(stage="x", rule_id="y"))
    with pytest.raises(ValidationError):
        DecisionRecord(**{**_record().model_dump(), "surprise": 1})
    assert _record().decision_confidence is None and _record().confidence_calibrated is False


def test_purge_text_also_blanks_quoted_evidence_spans_of_old_decisions(store):
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
    store.write_run([_record("old", decided_at=old, span=True), _record("new", run_id="r2", span=True)], {})
    assert store.purge_text(60) == (0, 1)
    assert store.purge_text(60) == (0, 0)
    assert store.get_latest_decision("old").evidence[0].span.text == ""          # quote gone…
    assert store.get_latest_decision("old").evidence[0].span.end == 9            # …offsets/provenance stay
    assert store.get_latest_decision("new").evidence[0].span.text == "Bac+5 req."  # recent untouched


def _db_bytes(path: str) -> bytes:
    import os

    out = open(path, "rb").read()
    wal = f"{path}-wal"
    return out + (open(wal, "rb").read() if os.path.exists(wal) else b"")


def test_purged_text_is_really_removed_from_the_database_file(store):
    secret = b"ZXQ-UNIQUE-SECRET-DESCRIPTION"
    payload = snapshot_payload(_job(secret.decode() + " " + "filler " * 40))
    h = snapshot_hash(payload)
    store.write_run([_record(snap=h)], {h: {"payload": payload, "job_id": "j1", "meta": JobMeta()}}, compress=False)
    assert secret in _db_bytes(store._db_path)                    # the test would be vacuous otherwise
    conn = sqlite3.connect(store._db_path)
    conn.execute("UPDATE job_snapshots SET last_seen_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    store.purge_text(60)
    assert secret not in _db_bytes(store._db_path)


def test_rereferenced_snapshot_refreshes_clock_and_restores_purged_text(store):
    payload = snapshot_payload(_job())
    h = snapshot_hash(payload)
    snap = {h: {"payload": payload, "job_id": "j1", "meta": JobMeta(title="Dev")}}
    store.write_run([_record(snap=h)], snap)
    conn = sqlite3.connect(store._db_path)
    conn.execute("UPDATE job_snapshots SET last_seen_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    store.purge_text(60)
    assert store.get_snapshot(h) is None
    # the same unchanged job is rejected again later: its text must come back and the clock restart
    assert store.write_run([_record(run_id="r2", snap=h)], snap)[1] == 0
    assert store.get_snapshot(h)["description"]
    assert store.purge_text(60) == (0, 0)                                  # freshly referenced: not purged


def test_snapshots_of_same_text_from_different_sources_are_distinct():
    a = Job(id="a", source="s1", title="Dev", description="same text " * 30)
    b = Job(id="b", source="s2", title="Dev", description="same text " * 30)
    assert snapshot_hash(snapshot_payload(a)) != snapshot_hash(snapshot_payload(b))
    assert snapshot_payload(a)["source"] == "s1"
