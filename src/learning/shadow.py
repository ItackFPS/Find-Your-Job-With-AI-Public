"""The personal model in shadow (ADR 0018, ADR 0019; stage L3.2).

At each run, after the production ranking is final, :func:`compute_shadow`:

1. aggregates the journal (signals received before the run started, after the reset marker);
2. fits the personal model and stores its summary in ``personal_model_state``;
3. computes the **challenger** ranking (production score + personal adjustment, gates re-applied)
   and stores both positions of every ranked offer in ``ranking_shadow``.

It returns the per-offer adjustment and the model version, which the runner stores in
``job_scores.personal_adjustment`` / ``personal_model_version``. Nothing it computes changes the
production ranking, ``final_score`` or the deterministic score (INV-3, INV-6). The caller fails open.

S4 columns of ``ranking_shadow`` (seed, pool size, inclusion probability) stay NULL until the
Découverte slots exist (stage L4).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.learning.config import PersonalModelConfig, load_personal_model_config
from src.learning.model import PersonalModel, fit, structured_values
from src.learning.signals import offer_signals
from src.models.job import Job

PROFILE_KEY = "__profile__"

DDL = """
CREATE TABLE IF NOT EXISTS personal_model_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    model_version TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    signals INTEGER NOT NULL,
    coverage REAL,
    reset_at TEXT,
    run_id TEXT,
    computed_at TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ranking_shadow (
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    production_position INTEGER NOT NULL,
    challenger_position INTEGER NOT NULL,
    personal_adjustment REAL NOT NULL,
    model_version TEXT NOT NULL,
    s4_seed TEXT,
    s4_pool_size INTEGER,
    s4_pi REAL,
    PRIMARY KEY (run_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_ranking_shadow_job ON ranking_shadow(job_id);
"""

Ranked = list[tuple[Job, float]]
GateCap = Callable[[Ranked, Ranked], Ranked]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _chunks(ids: Sequence[str], size: int = 500) -> Iterator[list[str]]:
    for i in range(0, len(ids), size):
        yield list(ids[i:i + size])


def _vectors(conn: sqlite3.Connection, ids: Sequence[str], model: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    if not model:
        return out
    try:
        for chunk in _chunks(sorted(set(ids))):
            for job_id, vec in conn.execute(
                    f"SELECT job_id, vector FROM job_embeddings WHERE model_name = ? "
                    f"AND job_id IN ({','.join('?' * len(chunk))})", (model, *chunk)):
                out[job_id] = json.loads(vec)
    except sqlite3.OperationalError:
        return {}
    return out


def _stored_values(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, dict[str, str | None]]:
    """Structured values of offers outside this run: their jobs row and latest stored factors."""
    out: dict[str, dict[str, str | None]] = {}
    for chunk in _chunks(sorted(set(ids))):
        marks = ",".join("?" * len(chunk))
        try:
            jobs = conn.execute(f"SELECT id, source, work_mode, remote, employment_type, salary_min FROM jobs "
                                f"WHERE id IN ({marks})", chunk).fetchall()
            factors: dict[str, dict[str, float]] = {}
            for job_id, breakdown in conn.execute(
                    f"SELECT js.job_id, js.score_breakdown_json FROM job_scores js "
                    f"JOIN pipeline_runs pr ON pr.id = js.run_id WHERE js.job_id IN ({marks}) "
                    f"ORDER BY pr.started_at", chunk):
                factors[job_id] = json.loads(breakdown or "{}")
        except sqlite3.OperationalError:
            continue
        for job_id, source, work_mode, remote, employment_type, salary_min in jobs:
            out[job_id] = structured_values(source=source, work_mode=work_mode, remote=bool(remote),
                                            employment_type=employment_type, salary_min=salary_min,
                                            factors=factors.get(job_id))
    return out


def _reset_at(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT learning_reset_at FROM app_state WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def build_model(conn: sqlite3.Connection, cfg: PersonalModelConfig, *, embedding_model: str,
                before: str, now: datetime, run_values: Mapping[str, Mapping[str, str | None]],
                reset_at: str | None) -> PersonalModel:
    """Fit the model on the journal as it stood at ``before``."""
    signals = offer_signals(conn, cfg, reset_at=reset_at, now=now, before=before)
    ids = [s.job_id for s in signals]
    vectors = _vectors(conn, [*ids, PROFILE_KEY], embedding_model)
    profile = vectors.pop(PROFILE_KEY, None)
    values = {**_stored_values(conn, [i for i in ids if i not in run_values]),
              **{i: dict(v) for i, v in run_values.items()}}
    return fit(signals, vectors, values, profile, cfg, embedding_model)


def compute_shadow(db_path: str | Path, run_id: str, production: Ranked,
                   factors_by_job_id: Mapping[str, Mapping[str, float]], gate_cap: GateCap, *,
                   embedding_model: str, started_at: datetime, config_path: str | Path,
                   now: datetime | None = None) -> tuple[dict[str, float], str]:
    """Fit, score and journal the challenger for this run → (adjustment by job id, model version).

    ``production`` is the final production ranking (never modified). ``gate_cap(production,
    challenger)`` re-applies the scoring gates to the challenger."""
    cfg = load_personal_model_config(config_path)
    now = now or datetime.now(UTC)
    run_values = {
        job.id: structured_values(source=job.source, work_mode=job.work_mode, remote=job.remote,
                                  employment_type=job.employment_type, salary_min=job.salary_min,
                                  factors=factors_by_job_id.get(job.id))
        for job, _ in production
    }
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        ensure_schema(conn)
        reset_at = _reset_at(conn)
        model = build_model(conn, cfg, embedding_model=embedding_model, before=started_at.isoformat(),
                            now=now, run_values=run_values, reset_at=reset_at)
        vectors = _vectors(conn, [job.id for job, _ in production], embedding_model) if model.n else {}
        adjustments = {job.id: model.adjustment(vectors.get(job.id), run_values[job.id]) for job, _ in production}
        challenger = sorted(((job, score + adjustments[job.id]) for job, score in production),
                            key=lambda p: p[1], reverse=True)
        challenger = gate_cap(list(production), challenger)
        prod_pos = {job.id: i for i, (job, _) in enumerate(production, start=1)}
        chal_pos = {job.id: i for i, (job, _) in enumerate(challenger, start=1)}
        version = model.version
        with conn:
            conn.execute("DELETE FROM ranking_shadow WHERE run_id = ?", (run_id,))
            conn.executemany(
                "INSERT INTO ranking_shadow (run_id, job_id, production_position, challenger_position, "
                "personal_adjustment, model_version) VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, jid, prod_pos[jid], chal_pos[jid], adjustments[jid], version) for jid in prod_pos])
            conn.execute(
                "INSERT OR REPLACE INTO personal_model_state (id, model_version, algorithm_version, config_digest, "
                "input_hash, signals, coverage, reset_at, run_id, computed_at, summary_json) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version, version.split("+", 1)[0], model.config_digest, model.input_hash, model.n, model.coverage,
                 reset_at, run_id, now.isoformat(timespec="seconds"), json.dumps(model.summary(), sort_keys=True)))
        return adjustments, version
    finally:
        conn.close()


def current_state(db_path: str | Path) -> dict[str, Any] | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM personal_model_state WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return None if row is None else {**dict(row), "summary": json.loads(row["summary_json"])}
