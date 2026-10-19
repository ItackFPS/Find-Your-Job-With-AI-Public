"""Does dashboard feedback reach the ranking? Only when the owner opts in (ADR 0009).

The dashboard records feedback in the append-only ``feedback_events`` journal. The personalization
stage reads the legacy ``job_feedback`` table, which explicit ``interested`` / ``not_interested``
feedback reaches only through ``personalization_bridge`` (``config/feedback.yaml``, default off).
These tests run the real pipeline twice around one dashboard answer and prove both sides:

- bridge on: the answer changes the next run's ranking (a non-zero personalization adjustment);
- bridge off: the answer is journaled and changes nothing;
- either way, accept/reject decisions are identical: personalization only re-ranks jobs that
  already passed every gate. The one thing it removes is a job the user themself marked
  ``not_interested`` (``exclude_actioned_jobs``): hidden from the ranking, never journaled as a
  rejection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.evaluation.dataset import load_dataset
from src.feedback.service import FeedbackService
from tests.test_decision_journal_orchestrator import DS, _config_dir, _FakeEmbeddingService


def _pipeline(db: Path, cfg: Path):
    from src.pipeline.orchestrator import run_pipeline

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg),
                              prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert result.status == "completed", result.error
    return result


def _around_one_answer(tmp: Path, bridge: bool, answer: str = "interested"):
    cfg = _config_dir(tmp, None, {"feedback.yaml": f"personalization_bridge: {str(bridge).lower()}\n"})
    db = tmp / "jobs.db"
    before = _pipeline(db, cfg)
    liked = before.scored_jobs[0].job_id
    FeedbackService(str(db), config_dir=str(cfg)).record_explicit(liked, answer, source="ui")
    after = _pipeline(db, cfg)
    return before, after, db


def _decisions(result) -> tuple[set[str], set[str]]:
    return {s.job_id for s in result.scored_jobs}, {r.job_id for r in result.rejection_records}


@pytest.mark.parametrize("bridge", [True, False])
def test_feedback_never_changes_accept_or_reject(tmp_path, bridge):
    before, after, _ = _around_one_answer(tmp_path, bridge)
    assert _decisions(after) == _decisions(before)


def test_with_the_bridge_on_dashboard_feedback_changes_the_next_ranking(tmp_path):
    import sqlite3

    before, after, db = _around_one_answer(tmp_path, True)
    assert all(s.personalization_adjustment == 0 for s in before.scored_jobs)
    assert any(s.personalization_adjustment > 0 for s in after.scored_jobs)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 1


def test_with_the_bridge_off_feedback_is_journaled_but_changes_nothing(tmp_path):
    import sqlite3

    before, after, db = _around_one_answer(tmp_path, False)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM feedback_events WHERE type = 'interested'").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "job_feedback" not in tables or conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 0
    assert all(s.personalization_adjustment == 0 for s in after.scored_jobs)
    assert [(s.job_id, s.final_score) for s in after.scored_jobs] == \
        [(s.job_id, s.final_score) for s in before.scored_jobs]


def test_a_job_the_user_declined_is_hidden_not_rejected(tmp_path):
    from src.decisions.storage import DecisionStorage

    before, after, db = _around_one_answer(tmp_path, True, "not_interested")
    declined = before.scored_jobs[0].job_id
    scored_before, rejected_before = _decisions(before)
    assert _decisions(after) == (scored_before - {declined}, rejected_before)
    st = DecisionStorage(str(db))
    try:
        record = st.get_decision_for_run(declined, after.run_id)
    finally:
        st.close()
    assert record.outcome == "accepted" and record.exposure is None


# -- the bridge is a derived, reversible view of the journal ---------------------------------------------

def _bridged_rows(db: Path) -> list[tuple[str, str, str]]:
    import sqlite3

    return sorted(sqlite3.connect(db).execute(
        "SELECT id, job_id, feedback_type FROM job_feedback WHERE substr(id, 1, 7) = 'bridge:'").fetchall())


def test_repeated_and_changed_answers_count_once_latest_wins(tmp_path):
    cfg = _config_dir(tmp_path, None, {"feedback.yaml": "personalization_bridge: true\n"})
    db = tmp_path / "jobs.db"
    first = _pipeline(db, cfg)
    job = first.scored_jobs[0].job_id
    svc = FeedbackService(str(db), config_dir=str(cfg))
    for _ in range(3):
        svc.record_explicit(job, "interested", source="ui")
    rows = _bridged_rows(db)
    assert [(j, t) for _, j, t in rows] == [(job, "interested")]          # three clicks, one preference
    last = svc.record_explicit(job, "not_interested", source="ui")
    assert _bridged_rows(db) == [(f"bridge:{last.event_id}", job, "not_interested")]   # traceable to the event


def test_clearing_the_bridge_restores_the_original_ranking_and_sync_rebuilds_it(tmp_path):
    from src.personalization.cli import record_feedback
    from src.personalization.models import FeedbackType
    from src.personalization.storage import PersonalizationStorage
    from src.storage.sqlite import Storage

    before, after, db = _around_one_answer(tmp_path, True)
    cfg = tmp_path / "config"
    assert any(s.personalization_adjustment > 0 for s in after.scored_jobs)
    # a row written by the legacy CLI is the user's own: never touched by the bridge commands
    st, ps = Storage(db_path=str(db)), PersonalizationStorage(db_path=str(db))
    st.init_db()
    ps.init_db()
    cli_row = record_feedback(st, ps, before.scored_jobs[-1].job_id, FeedbackType.IGNORED)
    st.close()
    ps.close()

    svc = FeedbackService(str(db), config_dir=str(cfg))
    assert svc.clear_bridge() == 1
    assert _bridged_rows(db) == []
    import sqlite3

    assert sqlite3.connect(db).execute("SELECT id FROM job_feedback").fetchall() == [(cli_row.id,)]
    assert svc.sync_bridge() == (1, 0)                                     # rebuilt from the journal
    assert len(_bridged_rows(db)) == 1


def test_turning_the_bridge_off_and_clearing_undoes_its_effect(tmp_path):
    before, _, db = _around_one_answer(tmp_path, True)
    cfg = tmp_path / "config"
    (cfg / "feedback.yaml").write_text("personalization_bridge: false\n", encoding="utf-8")
    FeedbackService(str(db), config_dir=str(cfg)).clear_bridge()
    again = _pipeline(db, cfg)
    assert all(s.personalization_adjustment == 0 for s in again.scored_jobs)
    assert [(s.job_id, s.final_score) for s in again.scored_jobs] == \
        [(s.job_id, s.final_score) for s in before.scored_jobs]
