"""The replay harness (Phase B, B1-C) runs the product's run path offline and never ships."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from src.pipeline.normalize import normalize_job
from src.storage.sqlite import Storage
from tests.fixtures.profiles import TEST_PROFILE_PATH
from tests.replay.harness import NetworkBlocked, ReplayCorpus, copy_database, no_network, replay_run
from tests.test_decision_journal_orchestrator import _FakeEmbeddingService

ROOT = Path(__file__).resolve().parents[1]
DS_JOBS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50" / "jobs.jsonl"

SOURCES_YAML = """sources:
  - name: replay-src
    type: eures
    active: true
    config: {}
  - name: france-travail
    type: france_travail
    active: false
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp: Path) -> Path:
    cfg = tmp / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    (cfg / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    return cfg


def _seed_db(tmp: Path) -> Path:
    """A database whose ``jobs`` table holds the 50 synthetic offers as stored by an earlier run."""
    db = tmp / "seed" / "jobs.db"
    db.parent.mkdir()
    st = Storage(db_path=str(db))
    st.init_db()
    for line in DS_JOBS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            st.upsert_job(normalize_job("replay-src", json.loads(line)))
    st.close()
    return db


# -- network guard -----------------------------------------------------------------------------------

def test_network_guard_refuses_a_non_loopback_connection() -> None:
    with no_network():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkBlocked):
                s.connect(("93.184.216.34", 443))
            with pytest.raises(NetworkBlocked):
                s.connect_ex(("8.8.8.8", 53))
        finally:
            s.close()


def test_network_guard_allows_loopback() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        with no_network():
            c = socket.create_connection(server.getsockname(), timeout=5)
            c.close()
    finally:
        server.close()


def test_network_guard_is_removed_after_the_block() -> None:
    real = socket.socket.connect
    with no_network():
        assert socket.socket.connect is not real
    assert socket.socket.connect is real


# -- corpus and copy ---------------------------------------------------------------------------------

def test_corpus_reads_stored_payloads_without_writing_the_source(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    before = _sha(db)
    corpus = ReplayCorpus.from_database(db)
    assert len(corpus) == 50 and set(corpus.by_source) == {"replay-src"}
    assert all(p.get("title") for p in corpus.by_source["replay-src"])
    assert _sha(db) == before


def test_copy_never_overwrites(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    copy_database(db, tmp_path / "work")
    with pytest.raises(FileExistsError):
        copy_database(db, tmp_path / "work")


# -- full replay -------------------------------------------------------------------------------------

def test_replay_runs_the_product_run_path_offline_twice(tmp_path: Path) -> None:
    seed = _seed_db(tmp_path)
    seed_hash = _sha(seed)
    corpus = ReplayCorpus.from_database(seed)
    work = copy_database(seed, tmp_path / "work")
    cfg = _config(tmp_path)
    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        first = replay_run(work, cfg, corpus)
        second = replay_run(work, cfg, corpus)
    for r in (first, second):
        assert r.status == "completed", r.error
        assert r.collected == 50
        assert r.by_source["replay-src"]["collected"] == 50
        assert 0 < r.accepted <= 50
        assert r.wall_seconds >= r.internal_seconds > 0
    assert first.accepted == second.accepted
    assert first.run_id != second.run_id
    assert _sha(seed) == seed_hash                      # the source database is never written
    st = Storage(db_path=str(work))
    try:
        stored = st._get_conn().execute("SELECT COUNT(*) FROM job_scores WHERE run_id = ?",
                                        (second.run_id,)).fetchone()[0]
    finally:
        st.close()
    assert stored == second.accepted                    # scores persisted by the real runner


def test_a_collector_that_reaches_for_the_network_fails_only_its_source(tmp_path: Path) -> None:
    """Belt and braces: even if a real collector ran inside a replay, the guard stops its request and
    the pipeline records that source as failed."""
    seed = _seed_db(tmp_path)
    work = copy_database(seed, tmp_path / "work")
    cfg = _config(tmp_path)

    class Leaky:
        def __init__(self, config=None, **_):
            self.name = (config or {}).get("name")

        def collect(self):
            socket.create_connection(("93.184.216.34", 443), timeout=5)

    from tests.replay import harness

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService), \
         patch.object(harness, "_replay_collector_class", lambda corpus: Leaky):
        r = replay_run(work, cfg, ReplayCorpus())
    assert r.status == "completed"
    assert r.by_source["replay-src"]["status"] == "failed"
    assert r.collected == 0


# -- never shipped -----------------------------------------------------------------------------------

def test_the_product_never_loads_the_harness() -> None:
    from src.collectors.registry import registry

    assert not any("replay" in name.lower() for name in registry.list_collectors())
    offenders = [p for p in (ROOT / "src").rglob("*.py")
                 if "tests.replay" in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == []
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["src*"]' in pyproject            # tests/ is not packaged


def test_command_line_writes_a_report(tmp_path: Path) -> None:
    from tests.replay.__main__ import main

    seed = _seed_db(tmp_path)
    cfg = _config(tmp_path)
    rc = main(["--source-db", str(seed), "--config", str(cfg), "--work-dir", str(tmp_path / "work"),
               "--runs", "1", "--fake-embeddings"])
    report = json.loads((tmp_path / "work" / "replay-report.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert report["corpus"] == {"replay-src": 50}
    assert report["runs"][0]["status"] == "completed"
    assert "wall_minus_internal_seconds" in report["runs"][0]
