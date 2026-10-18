"""When does the user's feedback answer a review-flagged decision? (``DecisionStorage.review_queue``)

A doubtful decision is asked about once per *episode*: the run of consecutive decisions with the same
outcome. An answer linked to any decision of the current episode, or unlinked feedback given after the
latest decision, answers it. A later run that changes the outcome starts a new episode, so a job the user
wanted that is rejected again after being accepted is reviewable again. Feedback explicitly linked to a
decision of another episode never answers the current one, whatever its timestamp. Weak behavioural
signals never answer anything.
"""

from __future__ import annotations

import pytest

from src.decisions.models import DecisionRecord, JobMeta
from src.decisions.storage import DecisionStorage
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage

JOB = "job-1"


@pytest.fixture
def stores(tmp_path):
    db = str(tmp_path / "jobs.db")
    ds, fs = DecisionStorage(db), FeedbackStorage(db)
    ds.init_db()
    fs.init_db()
    yield ds, fs
    ds.close()
    fs.close()


def _decide(ds: DecisionStorage, run: int, outcome: str, state: str = "review") -> str:
    did = f"d{run}"
    ds.write_run([DecisionRecord(
        decision_id=did, run_id=f"r{run}", job_id=JOB, decided_at=f"2026-10-0{run}T00:00:00+00:00",
        outcome=outcome, deciding_stage="filter" if outcome == "rejected" else "ranking", strategy_version="sv",
        signals={"assessment": {"state": state, "reasons": ["user wants it"], "conflicts": []}},
        job=JobMeta(title="Dev"))], {})
    return did


def _feedback(fs: FeedbackStorage, at: str, decision_id: str | None, type_: str = "interested",
              cls: str = "explicit") -> None:
    fs.append(FeedbackEvent(event_id=f"e-{at}-{type_}", job_id=JOB, decision_id=decision_id, signal_class=cls,
                            type=type_, source="ui", occurred_at=at))


def _queued(ds: DecisionStorage) -> bool:
    return JOB in {r.job_id for r in ds.review_queue()[1]}


def test_a_rejection_answered_once_is_not_asked_again_on_later_runs(stores):
    ds, fs = stores
    d1 = _decide(ds, 1, "rejected")
    assert _queued(ds)
    _feedback(fs, "2026-10-01T12:00:00+00:00", d1)
    for run in (2, 3, 4):                                   # rejected again and again: same episode
        _decide(ds, run, "rejected")
        assert not _queued(ds)


def test_a_job_rejected_again_after_being_accepted_is_reviewable_again(stores):
    ds, fs = stores
    d1 = _decide(ds, 1, "rejected")
    _feedback(fs, "2026-10-01T12:00:00+00:00", d1)          # "I want it" about the first rejection
    _decide(ds, 2, "accepted", state="accept")              # the strategy changed: accepted
    _decide(ds, 3, "rejected")                              # ...and later rejected again: a new episode
    assert _queued(ds)
    _feedback(fs, "2026-10-03T12:00:00+00:00", "d3")
    _decide(ds, 4, "rejected")
    assert not _queued(ds)


def test_feedback_linked_to_a_stale_decision_does_not_answer_a_newer_one(stores):
    ds, fs = stores
    d1 = _decide(ds, 1, "accepted", state="accept")
    _decide(ds, 2, "rejected")
    _feedback(fs, "2026-10-02T12:00:00+00:00", d1)          # clicked on the old (accepted) view, after run 2
    assert _queued(ds)


def test_unlinked_feedback_answers_only_when_it_follows_the_decision(stores):
    ds, fs = stores
    _feedback(fs, "2026-09-30T00:00:00+00:00", None)        # before any decision: says nothing about it
    _decide(ds, 1, "rejected")
    assert _queued(ds)
    _feedback(fs, "2026-10-01T00:00:00+00:00", None)        # same second as the decision: not an answer
    assert _queued(ds)
    _feedback(fs, "2026-10-01T00:00:01+00:00", None, type_="not_interested")
    assert not _queued(ds)


def test_weak_signals_never_answer(stores):
    ds, fs = stores
    d1 = _decide(ds, 1, "rejected")
    _feedback(fs, "2026-10-01T12:00:00+00:00", d1, type_="viewed", cls="behavior")
    _feedback(fs, "2026-10-01T12:00:01+00:00", d1, type_="apply_link_opened", cls="behavior")
    assert _queued(ds)


def test_a_newer_unflagged_decision_leaves_the_queue_without_an_answer(stores):
    ds, _ = stores
    _decide(ds, 1, "rejected")
    _decide(ds, 2, "rejected", state="reject")              # no longer doubtful
    assert not _queued(ds)
