"""Append-only feedback journal: storage, service, legacy-status mapping, bridge, CLI."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.decisions.models import DecisionRecord, JobMeta
from src.decisions.storage import DecisionStorage
from src.feedback.cli import inspect_statuses, main as cli_main
from src.feedback.config import FeedbackConfig, load_feedback_config
from src.feedback.models import FeedbackEvent
from src.feedback.service import FeedbackError, FeedbackService, canonical_status
from src.feedback.storage import FeedbackStorage
from src.models.job import Job
from src.outcomes.models import OutcomeEventType
from src.outcomes.storage import OutcomeEventStorage
from src.storage.sqlite import Storage


@pytest.fixture()
def db(tmp_path) -> str:
    path = str(tmp_path / "jobs.db")
    s = Storage(path)
    s.init_db()
    s.upsert_job(Job(id="j1", source="s", title="Dev", company="Acme", description="x" * 300, skills=["python"]))
    s.close()
    return path


def _decision(db: str, run="r1", job="j1", at=None, strategy="sv-aaa") -> DecisionRecord:
    rec = DecisionRecord(
        decision_id=f"d-{run}-{job}", run_id=run, job_id=job, strategy_version=strategy, outcome="accepted",
        deciding_stage="ranking", decided_at=at or datetime.now(UTC).isoformat(timespec="seconds"),
        job=JobMeta(title="Dev"))
    st = DecisionStorage(db)
    st.init_db()
    st.write_run([rec], {})
    st.close()
    return rec


def _event(**kw) -> FeedbackEvent:
    base = dict(event_id="e1", job_id="j1", signal_class="explicit", type="interested", source="ui",
                occurred_at="2026-10-01T00:00:00+00:00")
    return FeedbackEvent(**{**base, **kw})


def test_storage_is_append_only(db):
    st = FeedbackStorage(db)
    st.init_db()
    st.append(_event())
    assert st.list_for_job("j1")[0].event_id == "e1"
    conn = sqlite3.connect(db, isolation_level=None)
    for stmt in ("UPDATE feedback_events SET note = 'x'", "DELETE FROM feedback_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(stmt)
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        st.append(_event())                                    # duplicate event id
    st.close()


def test_no_weight_or_score_is_stored_at_insertion(db):
    FeedbackStorage(db).init_db()
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(feedback_events)")}
    assert not {c for c in cols if "weight" in c or "score" in c or "reliab" in c}
    assert {"job_id", "decision_id", "strategy_version", "signal_class", "type", "source", "occurred_at"} <= cols


def test_explicit_feedback_links_to_decision_and_strategy(db):
    d = _decision(db)
    e = FeedbackService(db).record_explicit("j1", "not_interested", ["too_far", "salary"], "commute", source="ui")
    assert (e.decision_id, e.strategy_version) == (d.decision_id, "sv-aaa")
    assert e.signal_class == "explicit" and e.reasons == ["too_far", "salary"] and e.note == "commute"
    assert e.metadata["decision_outcome"] == "accepted" and e.source == "ui"
    stored = FeedbackStorage(db)
    stored.init_db()
    assert stored.list_for_decision(d.decision_id)[0].event_id == e.event_id


def test_latest_decision_is_the_one_linked(db):
    _decision(db, run="r1", at="2026-01-01T00:00:00+00:00", strategy="sv-old")
    _decision(db, run="r2", at="2026-02-01T00:00:00+00:00", strategy="sv-new")
    assert FeedbackService(db).record_behavior("j1", "viewed").strategy_version == "sv-new"


def test_job_without_decision_is_recorded_with_a_marker(db):
    e = FeedbackService(db).record_explicit("j1", "interested")
    assert e.decision_id is None and e.strategy_version is None and e.metadata["no_decision"] is True


@pytest.mark.parametrize("type_,reasons", [
    ("interested", ["too_far"]), ("not_interested", ["interesting_despite_low_score"]),
    ("ranking_feedback", ["salary"]), ("nonsense", []), ("viewed", []),
])
def test_invalid_explicit_feedback_is_rejected(db, type_, reasons):
    with pytest.raises(FeedbackError):
        FeedbackService(db).record_explicit("j1", type_, reasons)


def test_note_limit_and_reason_dedup(db):
    with pytest.raises(FeedbackError, match="note"):
        FeedbackService(db).record_explicit("j1", "interested", note="x" * 501)
    assert FeedbackService(db).record_explicit("j1", "not_interested", ["too_far", "too_far"]).reasons == ["too_far"]


def test_behavior_events_and_validation(db):
    svc = FeedbackService(db)
    assert svc.record_behavior("j1", "apply_link_opened").signal_class == "behavior"
    with pytest.raises(FeedbackError):
        svc.record_behavior("j1", "interested")


def test_legacy_status_mapping_never_assumes_rejected_equals_denied():
    cfg = FeedbackConfig()
    assert canonical_status("waiting", cfg) == ("applied", {"awaiting_response": True})
    assert canonical_status("to_apply", cfg)[0] == "to_apply" and canonical_status("interview", cfg)[0] == "interview"
    for ambiguous in ("accepted", "rejected", "denied"):
        assert canonical_status(ambiguous, cfg)[0] is None          # unresolved until the owner decides
    resolved = FeedbackConfig(legacy_status_mapping={"rejected": "withdrawn", "denied": "employer_rejected"})
    assert canonical_status("rejected", resolved)[0] == "withdrawn"
    assert canonical_status("denied", resolved)[0] == "employer_rejected"
    assert canonical_status(None, cfg) == (None, {})


def test_status_change_preserves_legacy_values_and_adds_canonical(db):
    e = FeedbackService(db).record_status_change("j1", "applied", "denied")
    assert e.signal_class == "outcome" and e.type == "application_status_changed"
    assert e.metadata["legacy_from"] == "applied" and e.metadata["legacy_to"] == "denied"
    assert e.metadata["canonical_from"] == "applied" and e.metadata["canonical_to"] is None


def test_config_validation_and_resilience(tmp_path):
    assert load_feedback_config(tmp_path) == FeedbackConfig()
    (tmp_path / "feedback.yaml").write_text("legacy_status_mapping:\n  rejected: withdrawn\npersonalization_bridge: true\n")
    c = load_feedback_config(tmp_path)
    assert c.personalization_bridge and c.legacy_status_mapping == {"rejected": "withdrawn"}
    for bad in ("legacy_status_mapping: {rejected: nonsense}\n", "legacy_status_mapping: {bogus: applied}\n", "extra: 1\n"):
        (tmp_path / "feedback.yaml").write_text(bad)
        assert load_feedback_config(tmp_path) == FeedbackConfig()


def _job_feedback_rows(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def test_personalization_bridge_is_off_by_default_and_opt_in(db, tmp_path):
    FeedbackService(db).record_explicit("j1", "interested")
    assert _job_feedback_rows(db) == 0                              # rankings are NOT affected by default
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "feedback.yaml").write_text("personalization_bridge: true\n")
    svc = FeedbackService(db, str(cfg))
    svc.record_explicit("j1", "not_interested", ["off_domain"], source="api")
    svc.record_explicit("j1", "ranking_feedback", ["mis_ranked"])
    svc.record_behavior("j1", "viewed")
    assert _job_feedback_rows(db) == 1                              # only interested / not_interested bridge


def test_backfill_from_outcome_events_is_idempotent_and_links_the_decision_at_the_time(db):
    _decision(db, run="r1", at="2026-01-01T00:00:00+00:00", strategy="sv-then")
    _decision(db, run="r2", at="2026-03-01T00:00:00+00:00", strategy="sv-later")
    out = OutcomeEventStorage(db)
    out.init_db()
    ev = out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": None, "to": "applied"})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE outcome_events SET occurred_at = '2026-02-01T10:00:00'")    # naive timestamp, as stored today
    conn.commit()
    svc = FeedbackService(db)
    assert svc.backfill_from_outcome_events() == (1, 0)
    assert svc.backfill_from_outcome_events() == (0, 0)
    st = FeedbackStorage(db)
    st.init_db()
    e = st.list_for_job("j1")[0]
    assert e.source == "import" and e.occurred_at.startswith("2026-02-01T10:00:00")
    assert e.strategy_version == "sv-then"                          # decision in force when it happened
    assert e.metadata["outcome_event_id"] == ev.id and e.metadata["canonical_to"] == "applied"
    assert e.metadata["backfilled"] is True and e.metadata["mapping_hash"]


def test_backfill_does_not_duplicate_transitions_already_mirrored_live(db):
    """The live mirror and the backfill share the outcome_event_id reference (unique-indexed)."""
    out = OutcomeEventStorage(db)
    out.init_db()
    live = out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": None, "to": "applied"})
    old = out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": "applied", "to": "interview"})
    svc = FeedbackService(db)
    svc.record_status_change("j1", None, "applied", outcome_event_id=live.id)       # what the PATCH route does
    assert svc.backfill_from_outcome_events() == (1, 0)                              # only the pre-journal one
    st = FeedbackStorage(db)
    st.init_db()
    assert sorted(e.metadata["outcome_event_id"] for e in st.list_for_job("j1")) == sorted([live.id, old.id])
    with pytest.raises(sqlite3.IntegrityError):                                      # even under a race
        svc.record_status_change("j1", None, "applied", outcome_event_id=live.id)


@pytest.mark.parametrize("stamp", ["2026-02-01T10:00:00", "2026-02-01T10:00:00+00:00", "2026-02-01T12:00:00+02:00"])
def test_backfill_handles_naive_and_aware_timestamps(db, stamp):
    out = OutcomeEventStorage(db)
    out.init_db()
    out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": None, "to": "applied"})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE outcome_events SET occurred_at = ?", (stamp,))
    conn.commit()
    assert FeedbackService(db).backfill_from_outcome_events() == (1, 0)
    st = FeedbackStorage(db)
    st.init_db()
    assert st.list_for_job("j1")[0].occurred_at == "2026-02-01T10:00:00+00:00"


def test_backfill_skips_malformed_rows_and_reports_them(db):
    out = OutcomeEventStorage(db)
    out.init_db()
    out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": None, "to": "applied"})
    out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": "applied", "to": "interview"})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE outcome_events SET occurred_at = 'not-a-date' WHERE rowid = 1")
    conn.commit()
    assert FeedbackService(db).backfill_from_outcome_events() == (1, 1)


def test_backfill_propagates_real_database_errors(tmp_path):
    p = tmp_path / "bad.db"
    p.write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(sqlite3.DatabaseError):
        FeedbackService(str(p)).backfill_from_outcome_events()


def test_canonical_status_is_derived_at_read_time_from_the_current_mapping(db, tmp_path):
    """Events are immutable; resolving `denied` later must still apply to history."""
    e = FeedbackService(db).record_status_change("j1", "applied", "denied")
    assert e.metadata["canonical_to"] is None and e.metadata["legacy_to"] == "denied"
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "feedback.yaml").write_text("legacy_status_mapping:\n  denied: employer_rejected\n")
    later = FeedbackService(db, str(cfg))
    assert later.canonical_for(e) == "employer_rejected"            # same old event, new meaning
    assert later.record_status_change("j1", "applied", "denied").metadata["mapping_hash"] != e.metadata["mapping_hash"]


def test_explicit_decision_id_is_validated_and_wins_over_latest(db):
    _decision(db, run="r1", at="2026-01-01T00:00:00+00:00", strategy="sv-shown")
    _decision(db, run="r2", at="2026-02-01T00:00:00+00:00", strategy="sv-newer")
    svc = FeedbackService(db)
    shown = svc.record_explicit("j1", "interested", decision_id="d-r1-j1")           # what the user was looking at
    assert shown.strategy_version == "sv-shown"
    with pytest.raises(FeedbackError, match="does not exist"):
        svc.record_explicit("j1", "interested", decision_id="nope")
    _decision(db, run="r3", job="other")
    with pytest.raises(FeedbackError, match="does not exist"):
        svc.record_explicit("j1", "interested", decision_id="d-r3-other")            # another job's decision


def test_decision_store_failure_never_loses_the_feedback(db, monkeypatch):
    monkeypatch.setattr(DecisionStorage, "get_latest_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    e = FeedbackService(db).record_explicit("j1", "interested")
    assert e.decision_id is None and "locked" in e.metadata["link_error"]


def test_backfill_on_a_database_without_outcome_events(tmp_path):
    p = str(tmp_path / "empty.db")
    sqlite3.connect(p).close()
    assert FeedbackService(p).backfill_from_outcome_events() == (0, 0)


def test_inspect_statuses_is_read_only_and_reports_values(db, capsys):
    s = Storage(db)
    s.init_db()
    s.upsert_job(Job(id="j2", source="s", title="Ops", description="y" * 300))
    s.update_application_status("j1", "rejected")
    s.update_application_status("j2", "denied")
    s.close()
    out = OutcomeEventStorage(db)
    out.init_db()
    out.record_event("j1", OutcomeEventType.APPLICATION_STATUS_CHANGED, metadata={"from": "applied", "to": "rejected"})
    before = Path(db).read_bytes()
    report = inspect_statuses(db)
    assert "'rejected'" in report and "'denied'" in report and "'applied' -> 'rejected'" in report
    assert Path(db).read_bytes() == before
    assert cli_main(["inspect-statuses", "--db", db]) == 0
    assert cli_main(["inspect-statuses", "--db", str(Path(db).parent / "missing.db")]) == 2
