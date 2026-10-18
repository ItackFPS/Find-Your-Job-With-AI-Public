"""Experimental memory of the lab (``lab.db``): what was observed, believed, tested and concluded.

Items are immutable; their status is an append-only history (the latest row is current), so nothing
is ever silently rewritten. Four kinds are kept apart:

* ``evidence``    — a fact measured on a frozen dataset (e.g. "keyword X rejects 2 accept-labelled jobs");
* ``hypothesis``  — a proposed change and why it might help; not a belief until tested;
* ``observation`` — what an experiment actually showed (verdict, guardrails, metric deltas);
* ``conclusion``  — what follows: a recommendation for HUMAN review, or a refuted hypothesis.

Every item records the inputs it was derived from (dataset id + jobs/labels hashes + strategy
fingerprint). On recall an item is **stale** when those inputs differ from the current ones: it was
true of other data or another strategy and must be re-established before being relied on.
Statuses: ``active`` (current), ``confirmed``, ``rejected`` (refuted — kept, so it is not retried
blindly), ``superseded``, ``retracted``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from src.lab.store import LabStore

KINDS = ("evidence", "hypothesis", "observation", "conclusion")
STATUSES = ("active", "confirmed", "rejected", "superseded", "retracted")

_DDL = """
CREATE TABLE IF NOT EXISTS memory_items (
    item_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('evidence', 'hypothesis', 'observation', 'conclusion')),
    statement TEXT NOT NULL, details_json TEXT NOT NULL, key TEXT,
    dataset_id TEXT, dataset_jobs_sha256 TEXT, dataset_labels_sha256 TEXT, strategy_fingerprint TEXT,
    source TEXT NOT NULL, ref TEXT);
CREATE INDEX IF NOT EXISTS idx_memory_key ON memory_items(key);
CREATE TABLE IF NOT EXISTS memory_status (
    item_id TEXT NOT NULL, at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'confirmed', 'rejected', 'superseded', 'retracted')),
    reason TEXT NOT NULL, ref TEXT);
CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_status(item_id, at);
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dataset_id TEXT NOT NULL,
    dataset_jobs_sha256 TEXT NOT NULL, dataset_labels_sha256 TEXT NOT NULL,
    strategy_fingerprint TEXT NOT NULL, report_json TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS memory_items_no_update BEFORE UPDATE ON memory_items
    BEGIN SELECT RAISE(ABORT, 'memory items are immutable'); END;
CREATE TRIGGER IF NOT EXISTS memory_items_no_delete BEFORE DELETE ON memory_items
    BEGIN SELECT RAISE(ABORT, 'memory items are immutable'); END;
CREATE TRIGGER IF NOT EXISTS memory_status_no_update BEFORE UPDATE ON memory_status
    BEGIN SELECT RAISE(ABORT, 'memory status is append-only'); END;
CREATE TRIGGER IF NOT EXISTS memory_status_no_delete BEFORE DELETE ON memory_status
    BEGIN SELECT RAISE(ABORT, 'memory status is append-only'); END;
CREATE TRIGGER IF NOT EXISTS agent_runs_no_update BEFORE UPDATE ON agent_runs
    BEGIN SELECT RAISE(ABORT, 'agent runs are immutable'); END;
"""

_INPUT_KEYS = ("dataset_id", "dataset_jobs_sha256", "dataset_labels_sha256", "strategy_fingerprint")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LabMemory:
    def __init__(self, store: LabStore):
        self._conn = store._conn
        self._conn.executescript(_DDL)

    def add(self, kind: str, statement: str, *, inputs: dict[str, str], details: dict[str, Any] | None = None,
            key: str | None = None, source: str = "agent", ref: str | None = None,
            status: str = "active", reason: str = "recorded") -> str:
        if kind not in KINDS:
            raise ValueError(f"unknown memory kind {kind!r}")
        if not statement.strip():
            raise ValueError("a memory item needs a statement")
        item_id = f"mem-{uuid.uuid4().hex[:10]}"
        with self._conn:
            self._conn.execute(
                "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, _now(), kind, statement, json.dumps(details or {}, sort_keys=True, default=str), key,
                 *(inputs.get(k) for k in _INPUT_KEYS), source, ref))
            self._conn.execute("INSERT INTO memory_status VALUES (?,?,?,?,?)", (item_id, _now(), status, reason, ref))
        return item_id

    def set_status(self, item_id: str, status: str, reason: str, ref: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        if not reason.strip():
            raise ValueError("a status change needs a reason")
        if not self._conn.execute("SELECT 1 FROM memory_items WHERE item_id = ?", (item_id,)).fetchone():
            raise KeyError(f"unknown memory item {item_id}")
        with self._conn:
            self._conn.execute("INSERT INTO memory_status VALUES (?,?,?,?,?)", (item_id, _now(), status, reason, ref))

    def recall(self, *, kind: str | None = None, key: str | None = None,
               current: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Items (newest first) with their current status, status history and ``stale`` flag
        (recorded inputs differ from ``current``; ``None`` when no current inputs are given)."""
        where, params = [], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if key:
            where.append("key = ?")
            params.append(key)
        rows = self._conn.execute(
            f"SELECT * FROM memory_items {'WHERE ' + ' AND '.join(where) if where else ''} "
            "ORDER BY created_at DESC, rowid DESC", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d.pop("details_json"))
            history = [dict(h) for h in self._conn.execute(
                "SELECT status, at, reason, ref FROM memory_status WHERE item_id = ? ORDER BY at, rowid",
                (d["item_id"],)).fetchall()]
            d["status"] = history[-1]["status"]
            d["history"] = history
            d["stale"] = None if current is None else any(d[k] != current.get(k) for k in _INPUT_KEYS)
            out.append(d)
        return out

    def record_run(self, inputs: dict[str, str], report: dict[str, Any]) -> str:
        run_id = f"agent-{uuid.uuid4().hex[:10]}"
        with self._conn:
            self._conn.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?)",
                               (run_id, _now(), *(inputs[k] for k in _INPUT_KEYS),
                                json.dumps(report, sort_keys=True, default=str)))
        return run_id
