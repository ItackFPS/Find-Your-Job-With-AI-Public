"""I18 — SQLite storage for outcome events.

One new, additive table (``outcome_events``); no existing table is touched.
This module has no knowledge of scoring, ranking, or eligibility — it is a
pure record/retrieve service, so nothing here can influence them.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.outcomes.models import OutcomeEvent, OutcomeEventType


class OutcomeEventStorage:
    """Thin wrapper around SQLite for I18 outcome events."""

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outcome_events (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                run_id TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_outcome_events_job_id ON outcome_events(job_id);
            CREATE INDEX IF NOT EXISTS idx_outcome_events_type ON outcome_events(event_type);
            """
        )
        conn.commit()

    def record_event(
        self,
        job_id: str,
        event_type: OutcomeEventType,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OutcomeEvent:
        """Persist one event. Always inserts a new row — a job detail can
        legitimately be viewed more than once, and each view is a distinct,
        meaningful observation. Callers that need "only record on an actual
        change" semantics (e.g. application-status changes) decide that
        themselves before calling this — see src.api.routes.jobs's PATCH
        handler, which only calls this when the new status differs from the
        old one.
        """
        event = OutcomeEvent(job_id=job_id, event_type=event_type, run_id=run_id, metadata=metadata or {})
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO outcome_events (id, job_id, event_type, occurred_at, run_id, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.job_id,
                event.event_type.value,
                event.occurred_at.isoformat(),
                event.run_id,
                json.dumps(event.metadata),
            ),
        )
        conn.commit()
        return event

    def get_events_for_job(
        self, job_id: str, event_type: OutcomeEventType | None = None, limit: int = 100
    ) -> list[OutcomeEvent]:
        conn = self._get_conn()
        if event_type is not None:
            rows = conn.execute(
                """SELECT * FROM outcome_events WHERE job_id = ? AND event_type = ?
                   ORDER BY occurred_at DESC LIMIT ?""",
                (job_id, event_type.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outcome_events WHERE job_id = ? ORDER BY occurred_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count_events_for_job(self, job_id: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM outcome_events WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> OutcomeEvent:
        data = dict(row)
        return OutcomeEvent(
            id=data["id"],
            job_id=data["job_id"],
            event_type=OutcomeEventType(data["event_type"]),
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            run_id=data["run_id"],
            metadata=json.loads(data["metadata"]) if data.get("metadata") else {},
        )
