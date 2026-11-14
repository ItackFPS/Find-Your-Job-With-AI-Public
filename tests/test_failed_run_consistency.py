"""P-002 (Phase B B2): a run that fails after storage leaves the database consistent: the run is
``failed``, nothing refers to an offer without a row, and no offer counts a miss or expires."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.collectors.base import BaseCollector, CollectorResult
from src.pipeline.normalize import normalize_job
from src.storage.sqlite import Storage
from tests.test_decision_journal_orchestrator import _FakeEmbeddingService
from tests.test_job_expiry import NOW, _pipeline_config


def _raw(job_id: str, **kw) -> dict:
    return {"source_job_id": job_id, "canonical_url": f"https://board.example/{job_id}",
            "title": "Développeur Python", "company": "Acme", **kw}


class _Board(BaseCollector):
    def __init__(self, config=None, **_) -> None:
        super().__init__()

    def collect(self) -> CollectorResult:
        return CollectorResult(source_name="board", listing_complete=True, jobs=[
            _raw("new", description="Python, FastAPI, SQL. CDI à Lyon.", location="Lyon")])


def _run(tmp_path: Path, *, fail: bool):
    from src.pipeline.orchestrator import run_pipeline

    cfg = _pipeline_config(tmp_path)
    db = tmp_path / "jobs.db"
    gone = normalize_job("board", _raw("gone", description="Python", location="Lyon"))
    s = Storage(db_path=str(db))
    s.init_db()
    s.upsert_job(gone)
    s.record_provenance([gone], NOW)
    s.close()

    def boom(*a, **k):
        raise RuntimeError("injected failure after storage")

    patches = [patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService),
               patch("src.collectors.registry.registry.get_collector_class", lambda _t: _Board)]
    if fail:
        patches.append(patch("src.personalization.engine.apply_personalization", boom))
    for p in patches:
        p.start()
    try:
        result = run_pipeline(db_path=str(db), config_dir=str(cfg))
    finally:
        for p in reversed(patches):
            p.stop()
    return result, db, gone.id


def test_a_failure_after_storage_counts_no_miss_and_leaves_no_dangling_reference(tmp_path: Path) -> None:
    result, db, gone_id = _run(tmp_path, fail=True)
    assert result.status == "failed" and "injected failure" in (result.error or "")
    assert result.scored_jobs == [] and result.rejection_records == []
    conn = sqlite3.connect(db)
    try:
        missed = conn.execute("SELECT missed_runs, status FROM jobs WHERE id = ?", (gone_id,)).fetchone()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        decisions = conn.execute("SELECT COUNT(*) FROM job_decisions WHERE run_id = ?",
                                 (result.run_id,)).fetchone()[0] if "job_decisions" in tables else 0
        dangling = conn.execute(
            "SELECT COUNT(*) FROM job_decisions d WHERE d.outcome = 'accepted' "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = d.job_id)").fetchone()[0] \
            if "job_decisions" in tables else 0
    finally:
        conn.close()
    assert missed == (0, "active")          # the failed run is not evidence that the offer closed
    assert decisions == 0                   # the journal is written only by a completed run
    assert dangling == 0


def test_a_completed_run_still_counts_the_miss(tmp_path: Path) -> None:
    result, db, gone_id = _run(tmp_path, fail=False)
    assert result.status == "completed", result.error
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT missed_runs FROM jobs WHERE id = ?", (gone_id,)).fetchone()[0] == 1
    finally:
        conn.close()
