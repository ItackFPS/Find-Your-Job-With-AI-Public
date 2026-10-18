"""Read-only view of the evaluation harness and the experiment lab (ADR 0007, 0011).

GET /api/lab/runs             — evaluation run results (``data/benchmarks/runs/**.json``), newest first
GET /api/lab/experiments      — lab experiments with verdict, guardrails and metric comparison
GET /api/lab/memory           — the lab agent's memory items with their current status
GET /api/lab/active-strategy  — the human-approved ``active_strategy`` pointer history (``jobs.db``)

Production never imports ``src.evaluation`` or ``src.lab``: this module only reads their files with
read-only SQLite URIs and plain JSON, and tolerates them being absent or older/newer schemas.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/lab", tags=["lab"])


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.db_path).parent


def _lab_db(request: Request) -> Path:
    """``data/lab.db``, or — when only it exists — ``data/lab/lab.db``, where an OS-isolated lab agent keeps
    it (its account may write ``data/lab/`` but nothing beside ``jobs.db``; ADR 0011)."""
    explicit = getattr(request.app.state, "lab_db_path", None)
    if explicit:
        return Path(explicit)
    default, isolated = _data_dir(request) / "lab.db", _data_dir(request) / "lab" / "lab.db"
    return isolated if not default.is_file() and isolated.is_file() else default


def _runs_dir(request: Request) -> Path:
    return Path(getattr(request.app.state, "benchmark_runs_dir", None) or _data_dir(request) / "benchmarks" / "runs")


def _query(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Rows as dicts from a read-only connection; a missing file/table is simply no rows."""
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise
    finally:
        conn.close()


_METRICS = ("tp", "tn", "fp", "fn", "borderline", "accuracy", "precision", "recall", "unsafe_rejection_rate")


@router.get("/runs")
def list_runs(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    root = _runs_dir(request)
    runs, unreadable = [], 0
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            overall = d["metrics"]["overall"]
            runs.append({
                "file": path.relative_to(root).as_posix(), "run_id": d["run_id"], "created_at": d["created_at"],
                "dataset": {k: d["dataset"].get(k) for k in ("id", "version", "kind", "jobs_sha256", "labels_sha256")},
                "strategy": {"name": d["strategy"].get("name"), "fingerprint": d["strategy"].get("fingerprint")},
                "n_labelled": d["metrics"].get("n_labelled"), "overall": {k: overall.get(k) for k in _METRICS},
                "review_status": (d["metrics"].get("review") or {}).get("status"),
                "warnings": d.get("warnings", []),
            })
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            unreadable += 1
    runs.sort(key=lambda r: r["created_at"], reverse=True)
    return {"total": len(runs), "unreadable": unreadable, "runs": runs[:limit]}


@router.get("/experiments")
def list_experiments(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    rows = _query(_lab_db(request), """
        SELECT e.experiment_id, e.created_at, e.candidate_id, e.dataset_id, e.verdict, e.eligible_for_approval,
               e.result_json, c.overrides_json, c.status AS candidate_status, h.statement AS hypothesis
        FROM experiments e
        LEFT JOIN candidate_strategies c ON c.candidate_id = e.candidate_id
        LEFT JOIN hypotheses h ON h.hypothesis_id = c.hypothesis_id
        ORDER BY e.created_at DESC, e.rowid DESC LIMIT ?""", (limit,))
    out = []
    for r in rows:
        try:
            res = json.loads(r.pop("result_json") or "{}")
            overrides = json.loads(r.pop("overrides_json") or "{}")
        except ValueError:
            res, overrides = {}, {}
        cmp_ = res.get("comparison") or {}
        out.append({**r, "eligible_for_approval": bool(r["eligible_for_approval"]), "overrides": overrides,
                    # experiments recorded before the tier existed are not independently validated
                    "promotion_evidence": res.get("promotion_evidence") or (
                        "exploratory_only" if r["eligible_for_approval"] else "not_eligible"),
                    "independently_validated": res.get("independently_validated") is True,
                    "evidence_basis": res.get("evidence_basis"),
                    "explanation": res.get("explanation"), "fixed": res.get("fixed"), "broken": res.get("broken"),
                    "guardrails": {k: bool(v.get("passed")) for k, v in (res.get("guardrails") or {}).items()},
                    "delta": cmp_.get("delta"), "limitations": cmp_.get("limitations", [])})
    return {"experiments": out}


@router.get("/memory")
def list_memory(request: Request, kind: str | None = Query(default=None, pattern="^(evidence|hypothesis|observation|conclusion)$"),
                limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
    where, params = ("WHERE m.kind = ?", (kind,)) if kind else ("", ())
    rows = _query(_lab_db(request), f"""
        SELECT m.item_id, m.created_at, m.kind, m.statement, m.dataset_id, m.strategy_fingerprint, m.ref,
               (SELECT s.status FROM memory_status s WHERE s.item_id = m.item_id ORDER BY s.at DESC, s.rowid DESC LIMIT 1)
                   AS status
        FROM memory_items m {where} ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?""", (*params, limit))
    return {"items": rows}


@router.get("/active-strategy")
def active_strategy(request: Request) -> dict[str, Any]:
    from src.decisions.active_strategy import resolve_active_strategy

    rows = _query(Path(request.app.state.db_path),
                  "SELECT * FROM active_strategy ORDER BY set_at DESC, rowid DESC")
    pipeline = resolve_active_strategy(request.app.state.db_path,
                                       getattr(request.app.state, "config_dir", None) or "config").to_dict()
    return {"current": rows[0] if rows else None, "history": rows, "honoured_by_pipeline": True,
            "pipeline": pipeline,
            "note": (("The pipeline runs the approved bundle's filters.yaml and scoring.yaml."
                      + {"exploratory_only": " It was approved on EXPLORATORY evidence, not independent validation.",
                         "unrecorded": " Its approval evidence was not recorded."}.get(pipeline["approval_basis"], ""))
                     if pipeline["status"] == "active" else f"The pipeline runs on config/: {pipeline['reason']}.")}
