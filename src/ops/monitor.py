"""Self-monitoring base (ADR 0025, v5 stage L2): per-source metrics, two detectors, alerts.

After each completed pipeline run, ``after_run``:

1. records per-source daily metrics in ``ops_metrics_daily`` (offers collected; field completeness of
   the offers the run saw: title, description, company, location, publication date, contract type);
2. runs two detectors and keeps ``ops_alerts`` current:

   * ``source_volume_drop`` — the offers a source collected fall far below its usual level: a robust
     z-score < ``Z_THRESHOLD`` against the median and MAD of its previous runs over ``WINDOW_DAYS``,
     on ``PERSISTENCE`` consecutive runs; or 0 offers on those runs while the median is at least
     ``MIN_VOLUME``. Needs ``MIN_HISTORY`` earlier runs.
   * ``field_completeness_drop`` — a field is filled on ``COMPLETENESS_DROP`` (20 points) fewer of a
     source's offers than its median over the previous days (needs ``MIN_HISTORY`` days with at least
     ``MIN_OFFERS`` offers each, and ``MIN_OFFERS`` offers today).

An alert is opened once per (scope, code) and updated while the condition holds; it is resolved
automatically when the condition clears, or by the user. Nothing here changes a source, a rule or a
score (ADR 0025: everything but breakers, pacing and personal-ranking pauses is an alert or a
proposal). Fail-open: the runner logs any exception and the run keeps its status.

All thresholds are initial values (v5 §9, unapproved), adjustable through releases only.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

WINDOW_DAYS = 14
Z_THRESHOLD = -3.0
PERSISTENCE = 2
MIN_HISTORY = 5
MIN_VOLUME = 5
COMPLETENESS_DROP = 0.20
MIN_OFFERS = 10
RETENTION_DAYS = 400

FIELDS = ("title", "description", "company", "location", "published_at", "employment_type")
_SKIPPED = {"skipped", "paused"}

DDL = """
CREATE TABLE IF NOT EXISTS ops_metrics_daily (
    day TEXT NOT NULL, scope TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (day, scope, metric));
CREATE TABLE IF NOT EXISTS ops_alerts (
    alert_id TEXT PRIMARY KEY, raised_at TEXT NOT NULL, updated_at TEXT NOT NULL, scope TEXT NOT NULL,
    code TEXT NOT NULL, severity TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open', resolved_at TEXT, resolved_by TEXT);
CREATE INDEX IF NOT EXISTS idx_ops_alerts_open ON ops_alerts(status, scope, code);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def robust_z(value: float, history: list[float]) -> float | None:
    """``0.6745 · (value − median) / MAD``; None when the history is flat (MAD 0)."""
    med = statistics.median(history)
    mad = statistics.median(abs(h - med) for h in history)
    if mad == 0:
        return None
    return 0.6745 * (value - med) / mad


# -- metrics -------------------------------------------------------------------------------------

def _record_completeness(conn: sqlite3.Connection, started_at: str, day: str) -> None:
    cols = ", ".join(
        f"SUM(CASE WHEN {f} IS NOT NULL AND TRIM({f}) != '' THEN 1 ELSE 0 END) AS {f}" for f in FIELDS)
    rows = conn.execute(f"SELECT source, COUNT(*) AS n, {cols} FROM jobs WHERE last_seen_at >= ? GROUP BY source",
                        (started_at,)).fetchall()
    for r in rows:
        for f in FIELDS:
            conn.execute("INSERT OR REPLACE INTO ops_metrics_daily VALUES (?, ?, ?, ?, ?)",
                         (day, f"source:{r['source']}", f"completeness.{f}", (r[f] or 0) / r["n"], r["n"]))


def _record_volume(conn: sqlite3.Connection, by_source: dict[str, Any], day: str) -> None:
    for name, row in by_source.items():
        if not isinstance(row, dict) or row.get("status") in _SKIPPED:
            continue
        scope = f"source:{name}"
        prev = conn.execute("SELECT value, n FROM ops_metrics_daily WHERE day = ? AND scope = ? AND metric = 'collected'",
                            (day, scope)).fetchone()
        value = float(row.get("collected") or 0) + (prev["value"] if prev else 0)
        conn.execute("INSERT OR REPLACE INTO ops_metrics_daily VALUES (?, ?, 'collected', ?, ?)",
                     (day, scope, value, (prev["n"] if prev else 0) + 1))


# -- detectors -----------------------------------------------------------------------------------

def _runs(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, started_at, stats_json FROM pipeline_runs WHERE status = 'completed' "
                        "AND started_at >= ? ORDER BY started_at", (since,)).fetchall()
    out = []
    for r in rows:
        try:
            stats = json.loads(r["stats_json"] or "{}")
        except ValueError:
            continue
        out.append({"id": r["id"], "started_at": r["started_at"], "by_source": stats.get("by_source") or {}})
    return out


def detect_volume(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Per source scope: the alert evidence when the volume detector fires, else None."""
    series: dict[str, list[float]] = {}
    for run in runs:
        for name, row in run["by_source"].items():
            if isinstance(row, dict) and row.get("status") not in _SKIPPED:
                series.setdefault(name, []).append(float(row.get("collected") or 0))
    out: dict[str, dict[str, Any] | None] = {}
    for name, values in series.items():
        recent, history = values[-PERSISTENCE:], values[:-PERSISTENCE]
        if len(recent) < PERSISTENCE or len(history) < MIN_HISTORY:
            continue
        med = statistics.median(history)
        zs = [robust_z(v, history) for v in recent]
        zero = med >= MIN_VOLUME and all(v == 0 for v in recent)
        low = med >= MIN_VOLUME and all(z is not None and z < Z_THRESHOLD for z in zs)
        out[f"source:{name}"] = ({"source": name, "recent": recent, "median": med, "runs": len(history),
                                  "z": [None if z is None else round(z, 2) for z in zs]}
                                 if zero or low else None)
    return out


def detect_completeness(conn: sqlite3.Connection, day: str) -> dict[str, dict[str, Any] | None]:
    since = (datetime.fromisoformat(day) - timedelta(days=WINDOW_DAYS)).date().isoformat()
    today = conn.execute("SELECT scope, metric, value, n FROM ops_metrics_daily WHERE day = ? "
                         "AND metric LIKE 'completeness.%'", (day,)).fetchall()
    out: dict[str, dict[str, Any] | None] = {}
    for r in today:
        key = f"{r['scope']}|{r['metric'].split('.', 1)[1]}"
        hist = [h["value"] for h in conn.execute(
            "SELECT value FROM ops_metrics_daily WHERE scope = ? AND metric = ? AND day >= ? AND day < ? AND n >= ?",
            (r["scope"], r["metric"], since, day, MIN_OFFERS))]
        if len(hist) < MIN_HISTORY or r["n"] < MIN_OFFERS:
            continue
        med = statistics.median(hist)
        out[key] = ({"source": r["scope"].split(":", 1)[1], "field": r["metric"].split(".", 1)[1],
                     "today": round(r["value"], 3), "median": round(med, 3), "offers": r["n"], "days": len(hist)}
                    if med - r["value"] >= COMPLETENESS_DROP else None)
    return out


# -- alerts --------------------------------------------------------------------------------------

def _sync(conn: sqlite3.Connection, code: str, severity: str, found: dict[str, dict[str, Any] | None],
          now: str) -> None:
    for scope, evidence in found.items():
        row = conn.execute("SELECT alert_id FROM ops_alerts WHERE status = 'open' AND scope = ? AND code = ?",
                           (scope, code)).fetchone()
        if evidence is not None and row is None:
            conn.execute("INSERT INTO ops_alerts (alert_id, raised_at, updated_at, scope, code, severity, evidence_json) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (uuid.uuid4().hex, now, now, scope, code, severity,
                                                          json.dumps(evidence)))
        elif evidence is not None:
            conn.execute("UPDATE ops_alerts SET updated_at = ?, evidence_json = ? WHERE alert_id = ?",
                         (now, json.dumps(evidence), row["alert_id"]))
        elif row is not None:
            conn.execute("UPDATE ops_alerts SET status = 'resolved', resolved_at = ?, resolved_by = 'auto' "
                         "WHERE alert_id = ?", (now, row["alert_id"]))


def after_run(db_path: str | Path, run_id: str, *, now: datetime | None = None) -> None:
    """Record this run's metrics and update the alerts. Raises on error; the caller fails open."""
    now = now or datetime.now(UTC)
    conn = _connect(db_path)
    try:
        run = conn.execute("SELECT started_at, stats_json FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return
        day = now.date().isoformat()
        stats = json.loads(run["stats_json"] or "{}")
        with conn:
            _record_volume(conn, stats.get("by_source") or {}, day)
            _record_completeness(conn, run["started_at"], day)
            stamp = _iso(now)
            _sync(conn, "source_volume_drop", "warning",
                  detect_volume(_runs(conn, _iso(now - timedelta(days=WINDOW_DAYS)))), stamp)
            completeness = detect_completeness(conn, day)
            _sync(conn, "field_completeness_drop", "warning",
                  {k.replace("|", ":field:"): v for k, v in completeness.items()}, stamp)
            cutoff = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
            conn.execute("DELETE FROM ops_metrics_daily WHERE day < ?", (cutoff,))
            conn.execute("DELETE FROM ops_alerts WHERE status != 'open' AND resolved_at < ?", (_iso(now - timedelta(days=RETENTION_DAYS)),))
    finally:
        conn.close()


def alerts(db_path: str | Path, statuses: Iterable[str] = ("open",), limit: int = 100) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        st = list(statuses)
        rows = conn.execute(f"SELECT * FROM ops_alerts WHERE status IN ({','.join('?' * len(st))}) "
                            "ORDER BY updated_at DESC LIMIT ?", (*st, limit)).fetchall()
        return [{**dict(r), "evidence": json.loads(r["evidence_json"] or "{}")} for r in rows]
    finally:
        conn.close()


def resolve(db_path: str | Path, alert_id: str, *, now: datetime | None = None) -> bool:
    conn = _connect(db_path)
    try:
        with conn:
            cur = conn.execute("UPDATE ops_alerts SET status = 'resolved', resolved_at = ?, resolved_by = 'user' "
                               "WHERE alert_id = ? AND status = 'open'", (_iso(now or datetime.now(UTC)), alert_id))
        return cur.rowcount == 1
    finally:
        conn.close()
