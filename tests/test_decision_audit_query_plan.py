"""The audit queries run once per run: they must look decisions up by job, not by outcome."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from src.decisions.storage import DecisionStorage
from src.feedback.storage import FeedbackStorage


def _plan(store: DecisionStorage, where: str) -> str:
    sql = f"SELECT COUNT(*) FROM {store._LATEST} WHERE d.rn = 1 AND d.outcome = 'rejected' AND {where}"
    return "\n".join(row[3] for row in store._get_conn().execute("EXPLAIN QUERY PLAN " + sql))


def _store(path: Path, *, feedback: bool) -> DecisionStorage:
    store = DecisionStorage(str(path))
    store.init_db()
    if feedback:
        fs = FeedbackStorage(str(path))
        fs.init_db()
        fs.close()
    return store


def test_audited_episode_is_searched_by_job(tmp_path: Path) -> None:
    store = _store(tmp_path / "d.db", feedback=False)
    try:
        plan = _plan(store, store._EPISODE_AUDITED)
    finally:
        store.close()
    assert "SEARCH p USING INDEX idx_decisions_job" in plan
    assert "idx_decisions_outcome" not in plan


def test_unanswered_clause_searches_the_episode_by_job(tmp_path: Path) -> None:
    store = _store(tmp_path / "d.db", feedback=True)
    try:
        clause = store._unanswered_clause()
        assert clause, "the feedback table exists, so the clause must not be empty"
        plan = _plan(store, "1 = 1 " + clause)
    finally:
        store.close()
    assert "SEARCH p USING INDEX idx_decisions_job" in plan
    assert "idx_decisions_outcome" not in plan


class _OldSql(DecisionStorage):
    """The same queries without the ``+`` hint (the SQL before the fix), to compare result sets."""

    _EPISODE_AUDITED = DecisionStorage._EPISODE_AUDITED.replace("+p.outcome", "p.outcome")

    def _unanswered_clause(self) -> str:
        return super()._unanswered_clause().replace("+p.outcome", "p.outcome")


def _fill(path: Path, n_jobs: int, runs: int, seed: int) -> None:
    """Synthetic journal: ``runs`` runs over ``n_jobs`` jobs, outcomes flipping now and then, some
    decisions audited or flagged ``review``, some explicit feedback linked or unlinked."""
    rng = random.Random(seed)
    rows, events = [], []
    for r in range(runs):
        day = f"2026-10-{r + 1:02d}T08:00:00+00:00"
        for j in range(n_jobs):
            if rng.random() < 0.15:          # the job was not collected in this run
                continue
            outcome = "rejected" if rng.random() < 0.7 else "accepted"
            signals: dict = {}
            if outcome == "rejected" and rng.random() < 0.1:
                signals["audit"] = {"sampled": 1}
            if rng.random() < 0.05:
                signals["assessment"] = {"state": "review"}
            did = f"d{r}-{j}"
            rows.append((did, 1, f"run{r}", f"job{j}", day, outcome, "filter", "s1", None, None, 0,
                         "{}", "[]", json.dumps(signals), "[]"))
            if rng.random() < 0.03:
                linked = rng.random() < 0.5
                at = f"2026-10-{r + 1:02d}T{9 if rng.random() < 0.7 else 7:02d}:00:00+00:00"
                events.append((f"e{r}-{j}", 1, 1, f"job{j}", did if linked else None, None, "explicit",
                               "interested", "[]", None, "ui", at, "{}"))
    store = DecisionStorage(str(path))
    conn = store._get_conn()
    conn.executemany(
        """INSERT INTO job_decisions (decision_id, schema_version, run_id, job_id, decided_at, outcome,
           deciding_stage, strategy_version, fit_score, decision_confidence, confidence_calibrated,
           evidence_json, conflicts_json, signals_json, stage_trace_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.executemany(
        """INSERT INTO feedback_events (event_id, schema_version, reason_vocab_version, job_id, decision_id,
           strategy_version, signal_class, type, reasons_json, note, source, occurred_at, metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", events)
    conn.commit()
    store.close()


def test_old_and_new_sql_return_the_same_results(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    _store(db, feedback=True).close()
    _fill(db, n_jobs=400, runs=6, seed=7)
    new, old = DecisionStorage(str(db)), _OldSql(str(db))
    try:
        for outcome in (None, "rejected", "accepted"):
            n_total, n_page = new.review_queue(outcome=outcome, limit=1000)
            o_total, o_page = old.review_queue(outcome=outcome, limit=1000)
            assert n_total == o_total
            assert [r.decision_id for r in n_page] == [r.decision_id for r in o_page]
        assert new.open_audit_count() == old.open_audit_count() > 0
        ids = [f"job{j}" for j in range(400)]
        assert new.audit_ineligible_jobs(ids) == old.audit_ineligible_jobs(ids)
    finally:
        new.close()
        old.close()


def test_fifty_thousand_decisions_keep_the_job_index(tmp_path: Path) -> None:
    """On a journal of more than 50,000 decisions the plans stay on the job index and the review
    queries complete. Times are printed for the record, not asserted (CI machines vary)."""
    db = tmp_path / "d.db"
    _store(db, feedback=True).close()
    _fill(db, n_jobs=6000, runs=10, seed=11)
    store = DecisionStorage(str(db))
    try:
        assert store._get_conn().execute("SELECT COUNT(*) FROM job_decisions").fetchone()[0] >= 50_000
        assert "SEARCH p USING INDEX idx_decisions_job" in _plan(store, store._EPISODE_AUDITED)
        assert "SEARCH p USING INDEX idx_decisions_job" in _plan(store, "1 = 1 " + store._unanswered_clause())
        timings = {}
        for name, call in (("review_queue", lambda: store.review_queue(limit=50)),
                           ("open_audit_count", store.open_audit_count),
                           ("audit_ineligible_jobs",
                            lambda: store.audit_ineligible_jobs([f"job{j}" for j in range(500)]))):
            t0 = time.perf_counter()
            call()
            timings[name] = round(time.perf_counter() - t0, 3)
        print("journal query seconds on 50k+ decisions:", timings)
    finally:
        store.close()


def test_review_queue_page_and_total_match_separate_queries(tmp_path: Path) -> None:
    """The page carries the queue's total (one evaluation); it equals a separate COUNT, and a page
    past the end still reports the total."""
    db = tmp_path / "d.db"
    _store(db, feedback=True).close()
    _fill(db, n_jobs=400, runs=6, seed=3)
    store = DecisionStorage(str(db))
    try:
        base = f"""FROM {store._LATEST} WHERE d.rn = 1
                   AND (json_extract(d.signals_json, '$.assessment.state') = 'review'
                        OR {store._EPISODE_AUDITED}) {store._unanswered_clause()}"""
        expected = store._get_conn().execute(f"SELECT COUNT(*) {base}").fetchone()[0]
        ids = [r[0] for r in store._get_conn().execute(
            f"SELECT d.decision_id {base} ORDER BY d.decided_at DESC, d.job_id")]
        assert expected > 60
        total, page = store.review_queue(limit=50, offset=10)
        assert total == expected
        assert [r.decision_id for r in page] == ids[10:60]
        total, page = store.review_queue(limit=50, offset=expected + 5)
        assert (total, page) == (expected, [])
    finally:
        store.close()
