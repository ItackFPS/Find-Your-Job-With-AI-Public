"""The user's own feedback is a disagreement signal (ADR 0010, engine v2).

A rejected job the user wants becomes ``review`` (never a silent rejection); an accepted job the user
declined is a recorded conflict. Weak behaviour (views, clicks) never counts, and the production
decision itself is unchanged (shadow).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from src.decisions.disagreement import DisagreementConfig, Signals, assess
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage

CFG = DisagreementConfig()


def _solid_rejection(**kw) -> Signals:
    return Signals(outcome="rejected", reject_sources=("rule",), **kw)


# -- engine ---------------------------------------------------------------------------------------

def test_a_solid_rejection_of_a_job_the_user_wants_goes_to_review():
    a = assess(_solid_rejection(user_stance="wants", user_stance_basis="explicit:interested"), CFG)
    assert a.state == "review"
    assert any("explicit:interested" in r for r in a.reasons)
    assert any("vs pipeline rejection" in c for c in a.conflicts)


def test_without_user_feedback_the_solid_rejection_stands():
    assert assess(_solid_rejection(), CFG).state == "reject"
    assert assess(_solid_rejection(user_stance="declines", user_stance_basis="explicit:not_interested"), CFG).state \
        == "reject"                                                     # user agrees with the rejection


def test_an_accepted_job_the_user_declined_is_a_recorded_conflict():
    base = dict(outcome="accepted", rules_score=0.8, semantic=0.78, phase2=0.8, completeness=1.0)
    agree = assess(Signals(**base), CFG)
    declined = assess(Signals(**base, user_stance="declines", user_stance_basis="explicit:not_interested"), CFG)
    assert agree.state == "accept" and declined.state == "review"
    assert declined.confidence < agree.confidence
    assert any("vs pipeline accept" in c for c in declined.conflicts)
    assert assess(Signals(**base, user_stance="wants", user_stance_basis="status:applied"), CFG).state == "accept"


def test_the_rule_can_be_switched_off_but_never_turns_a_rejection_into_an_accept():
    off = DisagreementConfig(review_on_user_feedback=False)
    assert assess(_solid_rejection(user_stance="wants"), off).state == "reject"
    for stance in ("wants", "declines", None):
        assert assess(_solid_rejection(user_stance=stance), CFG).state in ("reject", "review")


# -- stance derivation ------------------------------------------------------------------------------

def _event(job: str, cls: str, type_: str, at: str, **meta) -> FeedbackEvent:
    return FeedbackEvent(event_id=str(uuid.uuid4()), job_id=job, signal_class=cls, type=type_, source="ui",  # type: ignore[arg-type]
                         occurred_at=at, metadata=meta)


@pytest.fixture()
def fs(tmp_path):
    s = FeedbackStorage(str(tmp_path / "jobs.db"))
    s.init_db()
    yield s
    s.close()


def test_latest_explicit_or_outcome_event_wins_and_behaviour_is_ignored(fs):
    for e in [
        _event("a", "explicit", "interested", "2026-10-01T00:00:00+00:00"),
        _event("a", "explicit", "not_interested", "2026-10-02T00:00:00+00:00"),
        _event("b", "behavior", "apply_link_opened", "2026-10-01T00:00:00+00:00"),
        _event("c", "explicit", "not_interested", "2026-10-01T00:00:00+00:00"),
        _event("c", "outcome", "application_status_changed", "2026-10-03T00:00:00+00:00", canonical_to="applied"),
        _event("d", "outcome", "application_status_changed", "2026-10-01T00:00:00+00:00", canonical_to="interview"),
        _event("d", "outcome", "application_status_changed", "2026-10-02T00:00:00+00:00", canonical_to="withdrawn"),
        _event("e", "outcome", "application_status_changed", "2026-10-01T00:00:00+00:00", canonical_to=None),
        _event("f", "explicit", "ranking_feedback", "2026-10-01T00:00:00+00:00"),
    ]:
        fs.append(e)
    got = fs.user_stances(["a", "b", "c", "d", "e", "f", "zzz"])
    assert {k: (v["stance"], v["basis"]) for k, v in got.items()} == {
        "a": ("declines", "explicit:not_interested"), "c": ("wants", "status:applied")}


def test_user_stances_without_a_feedback_table_is_empty(tmp_path):
    s = FeedbackStorage(str(tmp_path / "empty.db"))
    try:
        assert s.user_stances(["a"]) == {}
    finally:
        s.close()


# -- end to end: the real run_pipeline twice -----------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


def test_feedback_between_runs_flags_the_contradicting_decisions_without_changing_them(tmp_path):
    from src.decisions.storage import DecisionStorage
    from src.evaluation.dataset import load_dataset
    from src.feedback.service import FeedbackService
    from src.pipeline.orchestrator import run_pipeline
    from tests.test_decision_journal_orchestrator import _config_dir, _FakeEmbeddingService

    cfg = _config_dir(tmp_path, None)
    db = tmp_path / "jobs.db"
    jobs = load_dataset(DS).build_jobs()

    def run():
        with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
            r = run_pipeline(db_path=str(db), config_dir=str(cfg), prefetched_linkedin_jobs=jobs)
        assert r.status == "completed", r.error
        st = DecisionStorage(str(db))
        try:
            return r, {d.job_id: d for d in st.get_decisions_for_run(r.run_id)}
        finally:
            st.close()

    first, recs = run()
    rejected = next(j for j, d in recs.items() if d.outcome == "rejected"
                    and (d.signals.get("assessment") or {}).get("state") == "reject")
    accepted = next(j for j, d in recs.items() if d.outcome == "accepted")
    svc = FeedbackService(str(db), str(cfg))
    svc.record_explicit(rejected, "interested", reasons=["interesting_despite_low_score"])
    svc.record_explicit(accepted, "not_interested", reasons=["off_domain"])

    second, recs2 = run()
    assert [s.job_id for s in second.scored_jobs] == [s.job_id for s in first.scored_jobs]   # production unchanged
    r_ass, a_ass = recs2[rejected].signals["assessment"], recs2[accepted].signals["assessment"]
    assert recs2[rejected].outcome == "rejected" and r_ass["state"] == "review"
    assert r_ass["signals"]["user"]["basis"] == "explicit:interested"
    assert any("vs pipeline rejection" in c.description for c in recs2[rejected].conflicts)
    assert a_ass["state"] == "review" and any("vs pipeline accept" in c for c in a_ass["conflicts"])


def test_a_broken_feedback_table_never_blocks_the_journal(tmp_path):
    from src.decisions.recorder import DecisionRecorder

    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE feedback_events (nope TEXT)")       # wrong schema: the query fails
    rec = DecisionRecorder.__new__(DecisionRecorder)
    rec._db_path, rec._order = str(db), ["a"]
    assert rec._load_user_stances() == {}
