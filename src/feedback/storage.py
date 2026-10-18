"""SQLite storage of the append-only feedback journal (table ``feedback_events`` in jobs.db).

Append-only is enforced by triggers: UPDATE and DELETE abort. A correction is a NEW event.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.feedback.models import FeedbackEvent

# Canonical application statuses that mean the user went after the job. ``employer_rejected`` counts:
# the user applied. ``withdrawn`` clears an earlier stance (the user stopped pursuing it, for reasons
# that say nothing about fit). Behaviour events (views, clicks) are weak and never count.
PURSUED_STATUSES = frozenset({"to_apply", "applied", "interview", "offer", "accepted_offer", "employer_rejected"})


class FeedbackStorage:
    # DDL only needs to run once per db file per process (same pattern as Storage/RunStorage).
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_db(self) -> None:
        key = str(self._db_path.resolve())
        if key in self._initialized_paths and self._db_path.exists():
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feedback_events (
                event_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                reason_vocab_version INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                decision_id TEXT,
                strategy_version TEXT,
                signal_class TEXT NOT NULL,
                type TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                note TEXT,
                source TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_job ON feedback_events(job_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_feedback_decision ON feedback_events(decision_id);
            CREATE INDEX IF NOT EXISTS idx_feedback_strategy ON feedback_events(strategy_version);
            CREATE INDEX IF NOT EXISTS idx_feedback_class ON feedback_events(signal_class, type);
            -- one journal event per outcome_events row: live mirror and backfill can never duplicate
            CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_outcome_ref
                ON feedback_events(json_extract(metadata_json, '$.outcome_event_id'))
                WHERE json_extract(metadata_json, '$.outcome_event_id') IS NOT NULL;
            CREATE TRIGGER IF NOT EXISTS feedback_events_no_update
                BEFORE UPDATE ON feedback_events
                BEGIN SELECT RAISE(ABORT, 'feedback_events is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS feedback_events_no_delete
                BEFORE DELETE ON feedback_events
                BEGIN SELECT RAISE(ABORT, 'feedback_events is append-only'); END;
        """)
        conn.commit()
        self._initialized_paths.add(key)

    def append(self, e: FeedbackEvent) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """INSERT INTO feedback_events
                   (event_id, schema_version, reason_vocab_version, job_id, decision_id, strategy_version,
                    signal_class, type, reasons_json, note, source, occurred_at, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e.event_id, e.schema_version, e.reason_vocab_version, e.job_id, e.decision_id,
                 e.strategy_version, e.signal_class, e.type, json.dumps(e.reasons), e.note, e.source,
                 e.occurred_at, json.dumps(e.metadata, ensure_ascii=False, default=str)),
            )

    @staticmethod
    def _row(r: sqlite3.Row) -> FeedbackEvent:
        return FeedbackEvent(
            schema_version=r["schema_version"], reason_vocab_version=r["reason_vocab_version"],
            event_id=r["event_id"], job_id=r["job_id"], decision_id=r["decision_id"],
            strategy_version=r["strategy_version"], signal_class=r["signal_class"], type=r["type"],
            reasons=json.loads(r["reasons_json"]), note=r["note"], source=r["source"],
            occurred_at=r["occurred_at"], metadata=json.loads(r["metadata_json"]),
        )

    def list_for_job(self, job_id: str, limit: int = 200) -> list[FeedbackEvent]:
        rows = self._get_conn().execute(
            "SELECT * FROM feedback_events WHERE job_id = ? ORDER BY occurred_at DESC, rowid DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def list_for_decision(self, decision_id: str) -> list[FeedbackEvent]:
        rows = self._get_conn().execute(
            "SELECT * FROM feedback_events WHERE decision_id = ? ORDER BY occurred_at, rowid", (decision_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, **where: Any) -> int:
        clause = " AND ".join(f"{k} = ?" for k in where) or "1=1"
        return int(self._get_conn().execute(
            f"SELECT COUNT(*) FROM feedback_events WHERE {clause}", tuple(where.values())
        ).fetchone()[0])

    def user_stances(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Per job, what the user's own explicit feedback and application outcomes say → ``{job_id:
        {"stance": "wants" | "declines", "basis": "explicit:interested" | "status:applied" | ...,
        "at": occurred_at, "event_id": ...}}``. The latest qualifying event wins; jobs without one are
        omitted. A missing table yields ``{}``."""
        out: dict[str, dict[str, Any]] = {}
        conn = self._get_conn()
        for i in range(0, len(job_ids), 500):
            chunk = job_ids[i : i + 500]
            try:
                rows = conn.execute(
                    f"""SELECT event_id, job_id, signal_class, type, occurred_at, metadata_json
                        FROM feedback_events
                        WHERE job_id IN ({",".join("?" * len(chunk))})
                          AND (signal_class = 'outcome'
                               OR (signal_class = 'explicit' AND type IN ('interested', 'not_interested')))
                        ORDER BY occurred_at, rowid""", chunk).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return {}
                raise
            for r in rows:
                if r["signal_class"] == "explicit":
                    stance, basis = ("wants" if r["type"] == "interested" else "declines"), f"explicit:{r['type']}"
                else:
                    to = json.loads(r["metadata_json"]).get("canonical_to")
                    if to == "withdrawn":
                        out.pop(r["job_id"], None)
                        continue
                    if to not in PURSUED_STATUSES:
                        continue                    # unmapped/ambiguous status: no stance either way
                    stance, basis = "wants", f"status:{to}"
                out[r["job_id"]] = {"stance": stance, "basis": basis, "at": r["occurred_at"], "event_id": r["event_id"]}
        return out

    def latest_explicit(self, types: tuple[str, ...], job_ids: list[str] | None = None) -> dict[str, FeedbackEvent]:
        """Per job, its latest explicit event of one of ``types`` (all jobs when ``job_ids`` is None)."""
        conn = self._get_conn()
        where = f"signal_class = 'explicit' AND type IN ({','.join('?' * len(types))})"
        params: list[Any] = list(types)
        if job_ids is not None:
            if not job_ids:
                return {}
            where += f" AND job_id IN ({','.join('?' * len(job_ids))})"
            params += job_ids
        out: dict[str, FeedbackEvent] = {}
        for r in conn.execute(f"SELECT * FROM feedback_events WHERE {where} ORDER BY occurred_at, rowid", params):
            out[r["job_id"]] = self._row(r)
        return out

    def has_outcome_ref(self, outcome_event_id: str) -> bool:
        """True when this ``outcome_events`` row already has a journal event (live mirror or backfill)."""
        row = self._get_conn().execute(
            "SELECT 1 FROM feedback_events WHERE json_extract(metadata_json, '$.outcome_event_id') = ?",
            (outcome_event_id,),
        ).fetchone()
        return row is not None
