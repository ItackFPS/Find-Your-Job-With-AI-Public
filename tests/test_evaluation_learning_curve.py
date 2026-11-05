"""Offline learning curve of the personal model (ADR 0018/0019; stage L3.5)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from src.evaluation import learning_curve as lc
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage

T0 = datetime(2026, 9, 1, tzinfo=UTC)
MODEL = "test-mini"
CONFIG = Path("config/personalization.yaml")
DAYS = 30


def _journal(tmp_path: Path, *, positives: bool = True) -> str:
    """60 offers in two clusters; one run a day ranks all of them in a random production order; the
    user saves one offer of cluster A each day, after that day's run. Cluster A is the planted
    preference: production ignores it."""
    path = str(tmp_path / "jobs.db")
    st = FeedbackStorage(db_path=path)
    st.init_db()
    rng = np.random.default_rng(5)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pipeline_runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running', stats_json TEXT NOT NULL DEFAULT '{}', error TEXT);
        CREATE TABLE job_scores (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
            deterministic_score REAL, semantic_score REAL, final_score REAL,
            score_breakdown_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE job_embeddings (job_id TEXT NOT NULL, model_name TEXT NOT NULL, content_hash TEXT NOT NULL,
            vector TEXT NOT NULL, computed_at TEXT NOT NULL, PRIMARY KEY (job_id, model_name));
    """)
    jobs = [f"a{i}" for i in range(30)] + [f"b{i}" for i in range(30)]
    for j in jobs:
        base = np.zeros(8)
        base[0 if j[0] == "a" else 1] = 1.0
        vec = base + rng.normal(0, 0.15, 8)
        conn.execute("INSERT INTO job_embeddings VALUES (?, ?, 'h', ?, ?)", (j, MODEL, json.dumps(vec.tolist()), T0.isoformat()))
    for d in range(DAYS):
        run = f"r{d:02d}"
        conn.execute("INSERT INTO pipeline_runs (id, started_at, status) VALUES (?, ?, 'completed')",
                     (run, (T0 + timedelta(days=d)).isoformat()))
        for rank, j in enumerate(rng.permutation(jobs)):
            conn.execute("INSERT INTO job_scores (id, run_id, job_id, final_score) VALUES (?, ?, ?, ?)",
                         (f"{run}-{j}", run, j, 0.6 - 0.001 * rank))
    conn.commit()
    conn.close()
    if positives:
        for d in range(DAYS):
            at = (T0 + timedelta(days=d, hours=12)).isoformat(timespec="seconds")
            st.append(FeedbackEvent(event_id=f"e{d}", job_id=f"a{d}", signal_class="list", type="saved",
                                    reasons=[], source="ui", occurred_at=at, metadata={}))
    st.close()
    return path


def _digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_planted_preference_challenger_wins_grow_with_signals(tmp_path):
    path = _journal(tmp_path)
    before = _digest(path)
    r = lc.learning_curve(path, config_path=CONFIG, now=T0 + timedelta(days=DAYS))
    assert _digest(path) == before                                     # read-only
    assert r["status"] == "measured" and r["positives"] == DAYS and r["anchored"] == DAYS
    by = {b["prior_signals"]: b for b in r["by_prior_signals"]}
    assert by["0"]["discordant"] == 0                                   # no signal → adjustment exactly 0
    late = [b for k, b in by.items() if k in ("10-19", "20-49")]
    assert sum(b["challenger_wins"] for b in late) > sum(b["discordant"] - b["challenger_wins"] for b in late)
    assert r["challenger_wins"] > r["production_wins"]
    assert r["wording"] == {"key": "learning_curve.summary", "n": r["discordant"], "x": r["challenger_wins"]}
    assert r["embedding_model"] == MODEL


def test_no_signal_gives_no_rows(tmp_path):
    r = lc.learning_curve(_journal(tmp_path, positives=False), config_path=CONFIG)
    assert r["status"] == "no_positives" and r["anchored"] == 0 and r["by_prior_signals"] == []


def test_positive_without_an_earlier_run_is_not_anchored(tmp_path):
    path = _journal(tmp_path, positives=False)
    st = FeedbackStorage(db_path=path)
    st.append(FeedbackEvent(event_id="early", job_id="a1", signal_class="list", type="saved", reasons=[],
                            source="ui", occurred_at=(T0 - timedelta(days=1)).isoformat(), metadata={}))
    st.close()
    r = lc.learning_curve(path, config_path=CONFIG)
    assert r["status"] == "no_anchored_positives" and r["unanchored"] == 1


def test_tool_never_reads_calibration_labels():
    src = Path(lc.__file__).read_text(encoding="utf-8")
    assert "calibration" not in src.split('"""', 2)[2] and "heldout" not in src


def test_api_returns_counts(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import app

    path = _journal(tmp_path, positives=False)
    saved = app.state.db_path
    app.state.db_path = path
    try:
        r = TestClient(app).get("/api/quality/learning-curve").json()
    finally:
        app.state.db_path = saved
    assert r["status"] == "no_positives" and "wording" in r
