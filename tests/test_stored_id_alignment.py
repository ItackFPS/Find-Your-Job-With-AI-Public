"""P-031 (Phase B B2-A): an offer stored under one id and collected again under another id (same source
and content) keeps ONE id across the jobs row, the run's scores, the decision journal and expiry."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.collectors.base import BaseCollector, CollectorResult
from src.models.job import Job
from src.storage.sqlite import Storage
from tests.fixtures.profiles import TEST_PROFILE_PATH
from tests.test_decision_journal_orchestrator import _FakeEmbeddingService

ROOT = Path(__file__).resolve().parents[1]
DS_JOBS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50" / "jobs.jsonl"


def _job(id_: str, source: str, h: str) -> Job:
    j = Job(id=id_, source=source, title=f"T {id_}", description="d")
    j.content_hash = h
    return j


def _stored(tmp: Path, *jobs: Job) -> Storage:
    st = Storage(db_path=str(tmp / "jobs.db"))
    st.init_db()
    st.upsert_jobs_batch(list(jobs))
    return st


def test_a_new_id_with_stored_content_takes_the_stored_id(tmp_path: Path) -> None:
    st = _stored(tmp_path, _job("stored", "eures", "h1"), _job("other", "eures", "h2"))
    try:
        before = st._get_conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        variant = _job("new-id", "eures", "h1")
        same_hash_other_source = _job("x", "jobicy", "h1")
        known = _job("other", "eures", "h2")
        fresh = _job("fresh", "eures", "h9")
        aliases = st.align_stored_ids([variant, same_hash_other_source, known, fresh])
        assert aliases == {"new-id": "stored"}
        assert variant.id == "stored"
        assert (same_hash_other_source.id, known.id, fresh.id) == ("x", "other", "fresh")
        assert st._get_conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before   # nothing written
    finally:
        st.close()


def test_the_oldest_stored_row_wins(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    old, new = _job("b-old", "eures", "h1"), _job("a-new", "eures", "h1")
    old.first_seen_at = datetime(2026, 1, 1, tzinfo=UTC)
    new.first_seen_at = datetime(2026, 6, 1, tzinfo=UTC)
    st = Storage(db_path=str(tmp_path / "jobs.db"))
    st.init_db()
    try:
        st.upsert_jobs_batch([old])
        # a second row with the same content can exist from before the fix: insert it directly
        st.upsert_job(new)
        variant = _job("c", "eures", "h1")
        st.align_stored_ids([variant])
        assert variant.id == "b-old"
    finally:
        st.close()


# -- end to end ----------------------------------------------------------------------------------------

SOURCES = """sources:
  - name: eures
    type: eures
    active: true
    config: {}
"""


def _config(tmp: Path) -> Path:
    cfg = tmp / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    (cfg / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    return cfg


def _payloads(prefix: str) -> list[dict]:
    out = []
    for i, line in enumerate(DS_JOBS.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            raw = json.loads(line)
            raw["source_job_id"] = f"{prefix}-{i}"      # a new id each "run", same title and description
            out.append(raw)
    return out


def _collector(payloads: list[dict]) -> type[BaseCollector]:
    class Fake(BaseCollector):
        def __init__(self, config=None, **_):
            super().__init__()

        def collect(self) -> CollectorResult:
            return CollectorResult(source_name="eures", jobs=[dict(p) for p in payloads])

    return Fake


def _run(tmp: Path, cfg: Path, payloads: list[dict]):
    from src.pipeline.orchestrator import run_pipeline

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService), \
         patch("src.collectors.registry.registry.get_collector_class", lambda _t: _collector(payloads)):
        result = run_pipeline(db_path=str(tmp / "jobs.db"), config_dir=str(cfg))
    assert result.status == "completed", result.error
    return result


def test_a_run_collecting_stored_offers_under_new_ids_creates_no_orphan(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    first = _run(tmp_path, cfg, _payloads("run1"))
    stored_ids = {r.job_id for r in first.scored_jobs}
    assert stored_ids                                            # the first run stored offers

    second = _run(tmp_path, cfg, _payloads("run2"))
    assert second.stats.stored_id_alignment["aligned"] == len(stored_ids)
    conn = sqlite3.connect(tmp_path / "jobs.db")
    try:
        rows = {r[0] for r in conn.execute("SELECT id FROM jobs")}
        decisions = conn.execute("SELECT job_id, outcome FROM job_decisions WHERE run_id = ?",
                                 (second.run_id,)).fetchall()
    finally:
        conn.close()
    assert rows == stored_ids                                    # no second row for the same offers
    accepted = {job_id for job_id, outcome in decisions if outcome == "accepted"}
    assert accepted == {r.job_id for r in second.scored_jobs}
    assert accepted <= rows                                      # zero accepted decision without a row
    assert {r.job_id for r in second.scored_jobs} <= rows       # zero score without a row
    assert len({job_id for job_id, _ in decisions}) == len(decisions)   # one decision per offer
