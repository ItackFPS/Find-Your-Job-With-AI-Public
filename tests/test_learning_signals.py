"""Per-offer learning signal from the journal (ADR 0018, v5 AD-3.1; stage L3.1)."""

from __future__ import annotations

import itertools
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage
from src.learning.config import PersonalModelConfig, load_personal_model_config
from src.learning.signals import ALL_FEATURES, CONTENT, offer_signals

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
CFG = PersonalModelConfig()
_ids = itertools.count()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "jobs.db")
    st = FeedbackStorage(db_path=path)
    st.init_db()
    yield path, st
    st.close()


CLASS = {"viewed": "behavior", "apply_link_opened": "behavior", "saved": "list", "unsaved": "list",
         "hidden": "list", "unhidden": "list", "interested": "explicit", "not_interested": "explicit",
         "unsure": "explicit", "ranking_feedback": "explicit", "application_status_changed": "outcome"}


def ev(st: FeedbackStorage, job: str, typ: str, *, days_ago: float = 1.0, reasons=(), status=None, **extra):
    at = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    meta = {"canonical_to": status} if status else {}
    st.append(FeedbackEvent(event_id=f"e{next(_ids)}", job_id=job, signal_class=CLASS[typ], type=typ,
                            reasons=list(reasons), source="ui", occurred_at=at, metadata=meta, **extra))


def signals(path: str, **kw) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {s.job_id: s for s in offer_signals(conn, CFG, now=NOW, reset_at=kw.pop("reset_at", None), **kw)}
    finally:
        conn.close()


@pytest.mark.parametrize("typ,kw,y,features", [
    ("viewed", {}, 0.25, CONTENT),
    ("saved", {}, 1.0, ALL_FEATURES),
    ("apply_link_opened", {}, 1.2, ALL_FEATURES),
    ("interested", {}, 1.2, ALL_FEATURES),
    ("not_interested", {}, -1.2, CONTENT),
    ("application_status_changed", {"status": "to_apply"}, 1.0, ALL_FEATURES),
    ("application_status_changed", {"status": "applied"}, 1.5, ALL_FEATURES),
    ("application_status_changed", {"status": "interview"}, 2.0, ALL_FEATURES),
    ("application_status_changed", {"status": "accepted_offer"}, 2.0, ALL_FEATURES),
])
def test_signal_table(db, typ, kw, y, features):
    path, st = db
    ev(st, "j", typ, **kw)
    s = signals(path)["j"]
    assert (s.y, s.features) == (y, features)


@pytest.mark.parametrize("typ,kw", [
    ("hidden", {}),                                         # Hide without a reason is list management
    ("unsure", {}),
    ("application_status_changed", {"status": "employer_rejected"}),
    ("ranking_feedback", {"reasons": ["mis_ranked"]}),
])
def test_no_signal(db, typ, kw):
    path, st = db
    ev(st, "j", typ, **kw)
    assert signals(path) == {}


def test_opened_counts_once(db):
    path, st = db
    for d in (3, 2, 1):
        ev(st, "j", "viewed", days_ago=d)
    assert signals(path)["j"].y == 0.25


def test_reason_routes_only_to_its_feature(db):
    path, st = db
    ev(st, "far", "not_interested", reasons=["too_far"])
    ev(st, "hid", "hidden", reasons=["salary"])
    ev(st, "both", "not_interested", reasons=["too_far", "contract_type"])
    ev(st, "skills", "not_interested", reasons=["insufficient_skills"])   # no feature yet: moves nothing
    s = signals(path)
    assert (s["far"].y, s["far"].features) == (-1.2, frozenset({"distance"}))
    assert s["hid"].features == frozenset({"salary"})
    assert s["both"].features == frozenset({"distance", "contract"})
    assert "skills" not in s


def test_strongest_positive_wins_and_employer_rejection_keeps_it(db):
    path, st = db
    ev(st, "j", "saved", days_ago=5)
    ev(st, "j", "application_status_changed", status="applied", days_ago=4)
    ev(st, "j", "application_status_changed", status="employer_rejected", days_ago=3)
    s = signals(path)["j"]
    assert (s.y, s.basis) == (1.5, "status:applied")


def test_withdrawn_cancels_the_outcome_positive(db):
    path, st = db
    ev(st, "j", "application_status_changed", status="applied", days_ago=4)
    ev(st, "j", "application_status_changed", status="withdrawn", days_ago=3)
    assert signals(path) == {}


def test_unsure_clears_a_stance_and_unsave_clears_save(db):
    path, st = db
    ev(st, "a", "not_interested", days_ago=3)
    ev(st, "a", "unsure", days_ago=2)
    ev(st, "b", "saved", days_ago=3)
    ev(st, "b", "unsaved", days_ago=2)
    assert signals(path) == {}


def test_negative_holds_unless_a_strong_positive_follows(db):
    path, st = db
    ev(st, "a", "not_interested", days_ago=3)
    ev(st, "a", "viewed", days_ago=2)                       # opening does not undo "not interested"
    ev(st, "b", "not_interested", days_ago=3)
    ev(st, "b", "application_status_changed", status="applied", days_ago=2)
    ev(st, "c", "saved", days_ago=3)
    ev(st, "c", "not_interested", days_ago=2)
    s = signals(path)
    assert s["a"].y == -1.2 and s["b"].y == 1.5 and s["c"].y == -1.2


def test_superseded_events_are_ignored(db):
    path, st = db
    ev(st, "j", "saved", client_instance_id="tab", client_seq=2)
    ev(st, "j", "unsaved", client_instance_id="tab", client_seq=1)   # arrives late: superseded
    assert signals(path)["j"].y == 1.0


def test_reset_marker_and_before_filter(db):
    path, st = db
    ev(st, "old", "saved", days_ago=10)
    ev(st, "new", "saved", days_ago=1)
    marker = (NOW - timedelta(days=5)).isoformat(timespec="seconds")
    assert set(signals(path, reset_at=marker)) == {"new"}
    assert set(signals(path, before=marker)) == {"old"}


def test_recency_and_profile_change_discount(db):
    path, st = db
    ev(st, "j", "saved", days_ago=90)
    assert signals(path)["j"].weight == pytest.approx(0.5)
    changed = (NOW - timedelta(days=30)).isoformat()
    assert signals(path, profile_changed_at=changed)["j"].weight == pytest.approx(0.25)


def test_empty_or_missing_journal(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    assert offer_signals(conn, CFG, reset_at=None, now=NOW) == []
    conn.close()


def test_config_section_and_cap(tmp_path):
    p = tmp_path / "personalization.yaml"
    p.write_text("personal_model:\n  half_life_days: 30\n  weights: {opened: 0.1}\n", encoding="utf-8")
    cfg = load_personal_model_config(p)
    assert cfg.half_life_days == 30 and cfg.weights["opened"] == 0.1 and cfg.weights["saved"] == 1.0
    assert cfg.digest() != PersonalModelConfig().digest()
    assert load_personal_model_config(tmp_path / "missing.yaml") == PersonalModelConfig()
    assert CFG.cap(0) == 0.0 and 0 < CFG.cap(1) < CFG.cap(50) < CFG.cap_max
