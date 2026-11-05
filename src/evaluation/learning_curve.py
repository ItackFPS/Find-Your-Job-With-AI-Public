"""Offline learning curve of the personal model (ADR 0018, ADR 0019; stage L3.5).

Replays the journal in time order. For each positive (an offer whose aggregated signal reaches
y ≥ +1.0), it:

1. finds the anchor run: the latest run that ranked the offer and started at or before the
   positive's effective time;
2. fits the personal model on the journal **strictly before** that time (no leakage);
3. ranks that run's offers by production score and by production score + personal adjustment,
   and reports where each ranking placed the offer.

The result is counts only, never offer content: positives, anchored positives, discordant positives
(≥ 3 positions apart, positions beyond 100 counted as 101) and the challenger's wins, overall and by
how many signals the model had seen. Wording follows ADR 0019: "Sur N offres que vous avez
retenues, la version personnalisée les aurait classées plus haut dans X cas". It is a description of
agreement with observed choices, not proof that one ranking is better.

Approximations, stated: the run's production order is ``ranking_shadow.production_position`` when
stored, else ``job_scores.final_score``; the scoring gates' cap on personalization is not re-applied
offline. Calibration labels are never read (they live in evaluation datasets, not in the journal).

Read-only (``mode=ro``, ``query_only``); the app runs it in a child process (ADR 0007).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evaluation.export import open_readonly
from src.learning.config import load_personal_model_config
from src.learning.gates import DISCORDANCE, POSITION_CEILING
from src.learning.shadow import _reset_at, _stored_values, _vectors, build_model
from src.learning.signals import offer_signals

DEFAULT_CONFIG = Path("config/personalization.yaml")
DEFAULT_LIMIT = 500                    # most recent positives; bounded run time
BUCKETS = ((0, 0), (1, 4), (5, 9), (10, 19), (20, 49), (50, None))


@dataclass(frozen=True)
class CurvePoint:
    prior_signals: int
    production_position: int
    challenger_position: int

    @property
    def discordant(self) -> bool:
        p, c = min(self.production_position, POSITION_CEILING), min(self.challenger_position, POSITION_CEILING)
        return abs(p - c) >= DISCORDANCE

    @property
    def challenger_wins(self) -> bool:
        return self.discordant and self.challenger_position < self.production_position


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _embedding_model(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT model_name, COUNT(*) AS n FROM job_embeddings GROUP BY model_name "
                           "ORDER BY n DESC, model_name LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0]) if row else ""


def _anchor_run(conn: sqlite3.Connection, job_id: str, at: str) -> str | None:
    best: tuple[datetime, str] | None = None
    for run_id, started in conn.execute(
            "SELECT pr.id, pr.started_at FROM job_scores js JOIN pipeline_runs pr ON pr.id = js.run_id "
            "WHERE js.job_id = ?", (job_id,)):
        t = _parse(started)
        if t <= _parse(at) and (best is None or t > best[0]):
            best = (t, run_id)
    return best[1] if best else None


def _production(conn: sqlite3.Connection, run_id: str) -> list[tuple[str, float]]:
    """(job id, production score) in production order."""
    scores = {jid: float(s or 0.0) for jid, s in conn.execute(
        "SELECT job_id, final_score FROM job_scores WHERE run_id = ?", (run_id,))}
    try:
        stored = dict(conn.execute("SELECT job_id, production_position FROM ranking_shadow WHERE run_id = ?",
                                   (run_id,)).fetchall())
    except sqlite3.OperationalError:
        stored = {}
    if stored and set(stored) >= set(scores):
        order = sorted(scores, key=lambda j: stored[j])
    else:
        order = sorted(scores, key=lambda j: (-scores[j], j))
    return [(j, scores[j]) for j in order]


def learning_curve(db_path: str | Path, *, config_path: str | Path = DEFAULT_CONFIG,
                   embedding_model: str | None = None, limit: int = DEFAULT_LIMIT,
                   now: datetime | None = None) -> dict[str, Any]:
    cfg = load_personal_model_config(config_path)
    now = now or datetime.now(UTC)
    conn = open_readonly(db_path)
    try:
        reset_at = _reset_at(conn)
        model_name = embedding_model if embedding_model is not None else _embedding_model(conn)
        signals = offer_signals(conn, cfg, reset_at=reset_at, now=now)
        positives = sorted((s for s in signals if s.is_positive), key=lambda s: (_parse(s.effective_at), s.job_id))
        positives = positives[-limit:] if limit else positives
        points: list[CurvePoint] = []
        unanchored = 0
        for pos in positives:
            try:
                run_id = _anchor_run(conn, pos.job_id, pos.effective_at)
            except sqlite3.OperationalError:
                run_id = None
            if run_id is None:
                unanchored += 1
                continue
            ranked = _production(conn, run_id)
            ids = [j for j, _ in ranked]
            run_values = _stored_values(conn, ids)
            model = build_model(conn, cfg, embedding_model=model_name, before=pos.effective_at,
                                now=_parse(pos.effective_at), run_values=run_values, reset_at=reset_at)
            vectors = _vectors(conn, ids, model_name) if model.n else {}
            challenger = sorted(((j, s + model.adjustment(vectors.get(j), run_values.get(j, {}))) for j, s in ranked),
                                key=lambda p: p[1], reverse=True)
            prod_pos = {j: i for i, (j, _) in enumerate(ranked, start=1)}
            chal_pos = {j: i for i, (j, _) in enumerate(challenger, start=1)}
            points.append(CurvePoint(model.n, prod_pos[pos.job_id], chal_pos[pos.job_id]))
    finally:
        conn.close()
    return _summary(points, len(positives), unanchored, model_name)


def _bucket(n: int) -> str:
    for lo, hi in BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            return f"{lo}+" if hi is None else (f"{lo}" if lo == hi else f"{lo}-{hi}")
    return "?"


def _summary(points: list[CurvePoint], positives: int, unanchored: int, model_name: str) -> dict[str, Any]:
    disc = [p for p in points if p.discordant]
    wins = sum(p.challenger_wins for p in disc)
    by: dict[str, Counter[str]] = {}
    for p in points:
        c = by.setdefault(_bucket(p.prior_signals), Counter())
        c["anchored"] += 1
        c["discordant"] += p.discordant
        c["challenger_wins"] += p.challenger_wins
    order = [_bucket(lo) for lo, _ in BUCKETS]
    status = "no_positives" if positives == 0 else ("no_anchored_positives" if not points else "measured")
    return {
        "status": status,
        "positives": positives,
        "anchored": len(points),
        "unanchored": unanchored,
        "discordant": len(disc),
        "challenger_wins": wins,
        "production_wins": len(disc) - wins,
        "by_prior_signals": [{"prior_signals": b, **dict(by[b])} for b in order if b in by],
        "embedding_model": model_name or None,
        "wording": {"key": "learning_curve.summary", "n": len(disc), "x": wins},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m src.evaluation.learning_curve",
                                 description="Offline learning curve of the personal model (read-only)")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ns = ap.parse_args(argv)
    print(json.dumps(learning_curve(ns.db, config_path=ns.config, limit=ns.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
