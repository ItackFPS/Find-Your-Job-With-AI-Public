"""P4: feedback + decisions -> frozen, anonymised evaluation dataset (read-only on jobs.db)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime

import pytest

from src.decisions.models import DecisionRecord, JobMeta
from src.decisions.storage import DecisionStorage, snapshot_hash, snapshot_payload
from src.evaluation.dataset import load_dataset
from src.evaluation.export import ExportError
from src.evaluation.feedback_dataset import (
    MIN_LABELLED, build_feedback_dataset, derive_label, labelling_queue,
)
from src.feedback.config import FeedbackConfig
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage
from src.models.job import Job

CFG = FeedbackConfig()


def _ev(type_, cls="explicit", reasons=(), legacy_to=None) -> dict:
    return {"signal_class": cls, "type": type_, "reasons_json": json.dumps(list(reasons)),
            "metadata_json": json.dumps({"legacy_to": legacy_to} if legacy_to else {})}


def test_derive_label_rules():
    assert derive_label([_ev("interested")], CFG) == ("accept", "high", "explicit:interested")
    assert derive_label([_ev("not_interested", reasons=["too_far"])], CFG)[0] == "reject"
    assert derive_label([_ev("not_interested"), _ev("interested")], CFG)[0] == "accept"      # latest wins
    assert derive_label([_ev("viewed", "behavior"), _ev("apply_link_opened", "behavior")], CFG) is None
    assert derive_label([_ev("application_status_changed", "outcome", legacy_to="interview")], CFG)[:2] == ("accept", "medium")
    assert derive_label([_ev("application_status_changed", "outcome", legacy_to="rejected")], CFG) is None  # unmapped: never guessed
    contested = derive_label([_ev("not_interested"), _ev("application_status_changed", "outcome", legacy_to="applied")], CFG)
    assert contested[0] == "borderline" and contested[1] == "low"


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "jobs.db")
    ds, fs = DecisionStorage(path), FeedbackStorage(path)
    ds.init_db()
    fs.init_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for i in range(6):
        job = Job(id=f"j{i}", source="s", title=f"Dev at Acme {i}", company="Acme Corp",
                  description=f"Contact hr{i}@acme.com python " * 20, skills=["python"])
        payload = snapshot_payload(job)
        h = snapshot_hash(payload)
        meta = JobMeta(title=job.title, company="Acme Corp", source="s")
        review = i == 5
        rec = DecisionRecord(
            decision_id=f"d{i}", run_id="r", job_id=job.id, strategy_version="sv", outcome="accepted",
            deciding_stage="ranking", decided_at=now, snapshot_hash=h, job=meta,
            signals={"assessment": {"state": "review", "reasons": ["x"]}} if review else {},
            decision_confidence=0.3 if review else 0.9)
        ds.write_run([rec], {h: {"payload": payload, "job_id": job.id, "meta": meta}})
    kinds = {"j0": "interested", "j1": "not_interested", "j2": "viewed"}
    for jid, t in kinds.items():
        fs.append(FeedbackEvent(event_id=f"e-{jid}", job_id=jid, decision_id=f"d{jid[1:]}",
                                signal_class="behavior" if t == "viewed" else "explicit", type=t, source="ui",
                                occurred_at=now, reasons=["too_far"] if t == "not_interested" else []))
    fs.append(FeedbackEvent(event_id="e-ghost", job_id="ghost", signal_class="explicit", type="interested",
                            source="ui", occurred_at=now))
    ds.close()
    fs.close()
    return path


def test_dataset_is_anonymised_valid_and_flagged_insufficient(db, tmp_path, monkeypatch):
    out = tmp_path / "fb"
    os.makedirs(out.parent, exist_ok=True)
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)  # tmp is outside the repo
    info = build_feedback_dataset(db, out, config_dir="config", salt="s")
    assert info["status"] == "insufficient_evidence"
    assert info["labels"] == {"accept": 1, "reject": 1}
    assert info["skipped"] == {"no_label_signal": 1, "no_decision_record": 1}
    ds = load_dataset(out)
    blob = (out / "jobs.jsonl").read_text()
    assert "acme" not in blob.lower() and "@" not in blob and ds.manifest.job_count == 2
    assert all(lab.label_source == "derived" and "reliability=high" in lab.reason for lab in ds.labels.values())
    assert "SELECTION BIAS" in ds.manifest.notes and MIN_LABELLED == 100
    with pytest.raises(ExportError):
        build_feedback_dataset(db, out, config_dir="config")          # never overwrites


def test_a_residual_identifier_aborts_the_build_before_anything_is_written(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    monkeypatch.setattr("src.evaluation.feedback_dataset.scrub_text", lambda text, company, alias: text)  # broken scrub
    out = tmp_path / "leaky"
    with pytest.raises(ExportError, match="residual identifier"):
        build_feedback_dataset(db, out, config_dir="config", salt="s")
    assert not out.exists() or not any(out.iterdir())


def test_the_private_sidecar_records_anonymisation_for_the_audit(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    build_feedback_dataset(db, tmp_path / "fb", config_dir="config", salt="s")
    assert json.loads((tmp_path / "fb" / "PRIVATE_export_meta.json").read_text(encoding="utf-8"))["anonymized"] is True


def test_build_does_not_modify_the_database(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    before = (os.path.getsize(db), os.stat(db).st_mtime_ns)
    build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s")
    assert (os.path.getsize(db), os.stat(db).st_mtime_ns) == before
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        assert c.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 4


def test_labelling_queue_lists_review_items_without_explicit_feedback(db):
    q = labelling_queue(db)
    assert [r["job_id"] for r in q] == ["j5"] and q[0]["reasons"] == ["x"]


def test_rebuild_with_fixed_salt_and_as_of_is_byte_identical_and_pins_the_profile(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    prof = tmp_path / "profile.yaml"
    prof.write_text("name: x\n")
    # as_of after the fixture's events: only feedback not after as_of is used
    kw = dict(config_dir="config", salt="s", as_of="2099-01-01T00:00:00+00:00", profile_path=prof)
    a = build_feedback_dataset(db, tmp_path / "a", **kw)["manifest"]
    b = build_feedback_dataset(db, tmp_path / "b", **kw)["manifest"]
    assert a["job_count"] == 2 and a == b and (tmp_path / "a" / "profile.yaml").read_text() == "name: x\n"


def test_same_second_decisions_yield_one_queue_row_and_missing_tables_raise_export_error(db, tmp_path):
    ds = DecisionStorage(db)
    ds.init_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rec = DecisionRecord(decision_id="dup", run_id="r2", job_id="j5", strategy_version="sv", outcome="accepted",
                         deciding_stage="ranking", decided_at=now, job=JobMeta(title="t"),
                         signals={"assessment": {"state": "review", "reasons": ["x"]}})
    ds.write_run([rec], {})
    assert [r["job_id"] for r in labelling_queue(db)].count("j5") == 1
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    with pytest.raises(ExportError):
        labelling_queue(bare)


def test_label_uses_the_snapshot_the_deciding_event_reacted_to(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    ds, fs = DecisionStorage(db), FeedbackStorage(db)
    ds.init_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    job = Job(id="j0", source="s", title="CHANGED TITLE", company="Zed", description="rust " * 80)
    payload = snapshot_payload(job)
    h = snapshot_hash(payload)
    meta = JobMeta(title=job.title)
    ds.write_run([DecisionRecord(decision_id="dnew", run_id="r9", job_id="j0", strategy_version="sv",
                                 outcome="accepted", deciding_stage="ranking", decided_at=now, snapshot_hash=h, job=meta)],
                 {h: {"payload": payload, "job_id": "j0", "meta": meta}})
    fs.append(FeedbackEvent(event_id="e-late", job_id="j0", decision_id="dnew", signal_class="behavior",
                            type="viewed", source="ui", occurred_at=now))
    build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s")
    assert "CHANGED TITLE" not in (tmp_path / "o" / "jobs.jsonl").read_text()     # label came from d0's snapshot


# ---------------------------------------------------------------------------
# Freshness context, duplicates, decision context and reproducibility
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _stored(db: str, first_seen: dict[str, str]) -> None:
    """Minimal ``jobs`` rows: only what the builder reads (``id``, ``first_seen_at``)."""
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, first_seen_at TEXT)")
        c.executemany("INSERT INTO jobs VALUES (?, ?)", first_seen.items())


def _sidecar(out) -> dict:
    return json.loads((out / "PRIVATE_export_meta.json").read_text(encoding="utf-8"))


def test_jobs_carry_first_seen_at_when_stored_before_as_of(db, tmp_path, monkeypatch):
    from datetime import timedelta

    from src.pipeline.filter import freshness_reference

    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    as_of = datetime.now(UTC) + timedelta(days=1)
    _stored(db, {"j0": "2026-01-02T03:04:05+00:00", "j1": _iso(as_of + timedelta(days=1))})  # j1: after as_of
    build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s", as_of=_iso(as_of))
    meta = _sidecar(tmp_path / "o")
    real = {new: old for new, old in meta["id_map"].items()}
    jobs = {real[j["id"]]: j for j in map(json.loads, (tmp_path / "o" / "jobs.jsonl").read_text().splitlines())}
    assert jobs["j0"]["first_seen_at"] == "2026-01-02T03:04:05+00:00"
    assert "first_seen_at" not in jobs["j1"]                       # stored only after as_of: not known then
    assert meta["builder"] == "feedback-dataset" and meta["first_seen_at"] is True
    # the replay sees the same freshness basis production uses for an undated listing (ADR 0012)
    rebuilt = {j.id: j for j in load_dataset(tmp_path / "o").build_jobs()}
    j0 = rebuilt[next(n for n, o in real.items() if o == "j0")]
    assert j0.published_at is None and freshness_reference(j0)[1] == "first_seen_at"
    j1 = rebuilt[next(n for n, o in real.items() if o == "j1")]
    assert freshness_reference(j1) == (None, "unknown")             # never stored before as_of: new, passes


def test_first_seen_false_reproduces_the_previous_format(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    _stored(db, {"j0": "2026-01-02T03:04:05+00:00"})
    build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s", first_seen=False)
    assert "first_seen_at" not in (tmp_path / "o" / "jobs.jsonl").read_text()
    assert _sidecar(tmp_path / "o")["first_seen_at"] is False


def test_no_jobs_table_means_no_first_sighting(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    info = build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s")
    assert info["n_jobs"] == 2 and "first_seen_at" not in (tmp_path / "o" / "jobs.jsonl").read_text()


def test_the_sidecar_keeps_the_decision_each_label_answers(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s")
    meta = _sidecar(tmp_path / "o")
    by_real = {meta["id_map"][new]: d for new, d in meta["decisions"].items()}
    assert by_real["j0"]["decision_id"] == "d0" and by_real["j1"]["decision_id"] == "d1"
    assert {d["outcome"] for d in by_real.values()} == {"accepted"} and by_real["j0"]["strategy_version"] == "sv"
    assert "accepted" not in (tmp_path / "o" / "jobs.jsonl").read_text()     # the dataset itself stays blind


def test_feedback_after_as_of_is_ignored_so_a_rebuild_is_identical(db, tmp_path, monkeypatch):
    from datetime import timedelta

    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    as_of = datetime.now(UTC) + timedelta(hours=1)
    kw = dict(config_dir="config", salt="s", as_of=_iso(as_of))
    a = build_feedback_dataset(db, tmp_path / "a", **kw)
    fs = FeedbackStorage(db)
    fs.init_db()
    fs.append(FeedbackEvent(event_id="e-late", job_id="j3", decision_id="d3", signal_class="explicit",
                            type="interested", source="ui", occurred_at=_iso(as_of + timedelta(days=1))))
    fs.close()
    b = build_feedback_dataset(db, tmp_path / "b", **kw)
    assert a["manifest"] == b["manifest"] and b["skipped"]["event_after_as_of"] == 1
    c = build_feedback_dataset(db, tmp_path / "c", config_dir="config", salt="s",
                               as_of=_iso(as_of + timedelta(days=2)))
    assert c["n_jobs"] == a["n_jobs"] + 1                            # a later as_of does include it


def test_duplicate_listings_count_once(db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    ds, fs = DecisionStorage(db), FeedbackStorage(db)
    ds.init_db()
    fs.init_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for jid in ("k1", "k2"):        # the same listing collected twice (e.g. two sources)
        job = Job(id=jid, source=jid, title="Rust developer", company="Zed", description="rust systems " * 40)
        payload = snapshot_payload(job)
        h = snapshot_hash(payload)
        meta = JobMeta(title=job.title, source=jid, content_hash="same-content")
        ds.write_run([DecisionRecord(decision_id=f"d-{jid}", run_id="r", job_id=jid, strategy_version="sv",
                                     outcome="accepted", deciding_stage="ranking", decided_at=now,
                                     snapshot_hash=h, job=meta)], {h: {"payload": payload, "job_id": jid, "meta": meta}})
        fs.append(FeedbackEvent(event_id=f"e-{jid}", job_id=jid, decision_id=f"d-{jid}", signal_class="explicit",
                                type="interested", source="ui", occurred_at=now))
    ds.close()
    fs.close()
    info = build_feedback_dataset(db, tmp_path / "o", config_dir="config", salt="s")
    assert info["labels"]["accept"] == 2 and info["skipped"]["duplicate_content"] == 1   # j0 + k1, not k2
    assert sorted(_sidecar(tmp_path / "o")["id_map"].values()) == ["j0", "j1", "k1"]


@pytest.mark.parametrize("legacy", [False, True])
def test_audit_rebuilds_a_feedback_dataset_with_its_own_builder(db, tmp_path, monkeypatch, legacy):
    from src.evaluation.audit import audit_dataset

    monkeypatch.setattr("src.evaluation.feedback_dataset.assert_not_committable", lambda p: None)
    monkeypatch.setattr("src.evaluation.audit.assert_not_committable", lambda p: None)
    _stored(db, {"j0": "2026-01-02T03:04:05+00:00"})
    out = tmp_path / "o"
    build_feedback_dataset(db, out, config_dir="config", salt="s", as_of=_iso(datetime.now(UTC)),
                           first_seen=not legacy)
    if legacy:      # a sidecar written before `builder` / `first_seen_at` were recorded
        meta = _sidecar(out)
        for k in ("builder", "first_seen_at", "decisions"):
            meta.pop(k)
        (out / "PRIVATE_export_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    check = audit_dataset(out, db=db)["checks"]["reproducible"]
    assert check["ok"], check
