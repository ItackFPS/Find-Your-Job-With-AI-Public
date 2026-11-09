"""The audit queries run once per run: they must look decisions up by job, not by outcome."""

from __future__ import annotations

from pathlib import Path

from src.decisions.storage import DecisionStorage


def _plan(store: DecisionStorage, where: str) -> str:
    sql = f"SELECT COUNT(*) FROM {store._LATEST} WHERE d.rn = 1 AND d.outcome = 'rejected' AND {where}"
    return "\n".join(row[3] for row in store._get_conn().execute("EXPLAIN QUERY PLAN " + sql))


def test_audited_episode_is_searched_by_job(tmp_path: Path) -> None:
    store = DecisionStorage(tmp_path / "d.db")
    store.init_db()
    try:
        plan = _plan(store, store._EPISODE_AUDITED)
    finally:
        store.close()
    assert "SEARCH p USING INDEX idx_decisions_job" in plan
    assert "idx_decisions_outcome" not in plan
