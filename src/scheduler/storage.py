"""SQLite storage for the generic scheduler.

Two tables (additive — never modifies existing tables):
  schedule_definitions  — what should be scheduled
  schedule_executions   — what actually ran

All timestamps are ISO 8601 UTC strings.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.scheduler.models import ScheduleDefinition

logger = logging.getLogger(__name__)

# Running executions older than this are considered stale (process died).
_STALE_HOURS = 4


class SchedulerStorage:
    """SQLite-backed storage for schedule definitions and execution history.

    Each instance owns its own connection.  Callers should create a new
    instance per thread (or close/reopen between threads) to avoid sharing
    a connection across threads.

    Uses isolation_level=None (autocommit) so that explicit BEGIN IMMEDIATE
    transactions work correctly in try_reserve_execution.
    """

    def __init__(
        self,
        db_path: str = "data/jobs.db",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        # Source of every timestamp written or compared here (tests inject one).
        self._now: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # -- connection -----------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,   # autocommit; explicit BEGIN used where needed
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")  # 5 s before SQLITE_BUSY
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema ---------------------------------------------------------------

    def init_db(self) -> None:
        """Create tables if they do not exist.  Safe to call multiple times."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schedule_definitions (
                id                   TEXT PRIMARY KEY,
                source_id            TEXT NOT NULL,
                label                TEXT NOT NULL,
                enabled              INTEGER NOT NULL DEFAULT 1,
                startup_policy       TEXT NOT NULL DEFAULT 'run_once_per_day',
                retry_max            INTEGER NOT NULL DEFAULT 0,
                retry_delay_seconds  INTEGER NOT NULL DEFAULT 300,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule_executions (
                id               TEXT PRIMARY KEY,
                definition_id    TEXT NOT NULL,
                trigger          TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'running',
                attempt          INTEGER NOT NULL DEFAULT 1,
                started_at       TEXT NOT NULL,
                finished_at      TEXT,
                duration_seconds REAL,
                error            TEXT,
                run_ref          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sched_exec_def
                ON schedule_executions(definition_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sched_exec_status
                ON schedule_executions(status);
        """)

    # -- schedule_definitions -------------------------------------------------

    def upsert_definition(self, defn: dict[str, Any]) -> None:
        """Insert or update a schedule definition by id."""
        conn = self._get_conn()
        now = self._now().isoformat()
        conn.execute(
            """INSERT INTO schedule_definitions
               (id, source_id, label, enabled, startup_policy,
                retry_max, retry_delay_seconds, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 source_id           = excluded.source_id,
                 label               = excluded.label,
                 enabled             = excluded.enabled,
                 startup_policy      = excluded.startup_policy,
                 retry_max           = excluded.retry_max,
                 retry_delay_seconds = excluded.retry_delay_seconds,
                 updated_at          = excluded.updated_at
               WHERE
                 source_id           != excluded.source_id OR
                 label               != excluded.label OR
                 enabled             != excluded.enabled OR
                 startup_policy      != excluded.startup_policy OR
                 retry_max           != excluded.retry_max OR
                 retry_delay_seconds != excluded.retry_delay_seconds
            """,
            (
                defn["id"],
                defn["source_id"],
                defn["label"],
                1 if defn.get("enabled", True) else 0,
                defn.get("startup_policy", "run_once_per_day"),
                int(defn.get("retry_max", 0)),
                int(defn.get("retry_delay_seconds", 300)),
                now,
                now,
            ),
        )

    def seed_from_yaml(self, path: str | Path) -> int:
        """Load schedule definitions from YAML, upserting each.  Returns count."""
        import yaml

        p = Path(path)
        if not p.exists():
            logger.debug("Scheduler: no schedules config at %s — skipping seed.", p)
            return 0
        with p.open() as f:
            raw = yaml.safe_load(f) or {}
        schedules = raw.get("schedules") or []
        count = 0
        for entry in schedules:
            if not entry.get("id") or not entry.get("source_id"):
                logger.warning("Scheduler: skipping schedule entry without id/source_id: %s", entry)
                continue
            self.upsert_definition(entry)
            count += 1
        logger.info("Scheduler: seeded %d schedule definition(s) from %s.", count, p)
        return count

    def get_enabled_definitions(self) -> list[ScheduleDefinition]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM schedule_definitions WHERE enabled = 1"
        ).fetchall()
        return [_row_to_definition(dict(r)) for r in rows]

    def get_definition(self, definition_id: str) -> ScheduleDefinition | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM schedule_definitions WHERE id = ?", (definition_id,)
        ).fetchone()
        return _row_to_definition(dict(row)) if row else None

    def list_all_definitions(self) -> list[ScheduleDefinition]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM schedule_definitions ORDER BY id").fetchall()
        return [_row_to_definition(dict(r)) for r in rows]

    # -- schedule_executions --------------------------------------------------

    def get_last_completed_execution(
        self,
        definition_id: str,
        since: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent completed execution, optionally filtered by start time."""
        conn = self._get_conn()
        if since is not None:
            row = conn.execute(
                """SELECT * FROM schedule_executions
                   WHERE definition_id = ? AND status = 'completed' AND started_at >= ?
                   ORDER BY started_at DESC LIMIT 1""",
                (definition_id, since.isoformat()),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM schedule_executions
                   WHERE definition_id = ? AND status = 'completed'
                   ORDER BY started_at DESC LIMIT 1""",
                (definition_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_last_execution(self, definition_id: str) -> dict[str, Any] | None:
        """Return the most recent execution of any status."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM schedule_executions
               WHERE definition_id = ?
               ORDER BY started_at DESC LIMIT 1""",
            (definition_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_executions(
        self, definition_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if definition_id:
            rows = conn.execute(
                """SELECT * FROM schedule_executions
                   WHERE definition_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (definition_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM schedule_executions
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def try_reserve_execution(
        self,
        definition_id: str,
        trigger: str,
        no_completed_since: datetime | None = None,
        attempt: int = 1,
    ) -> str | None:
        """Atomically check and reserve an execution slot.

        Returns a new execution id if the slot was acquired, or None if:
        - a non-stale 'running' execution already exists, OR
        - a 'completed' execution exists since no_completed_since (run_once_per_day guard).

        ``attempt`` is recorded as-is (1 = first attempt of the day; the
        scheduler passes failed_today + 1 for a retry, issue #68).

        Uses BEGIN IMMEDIATE to prevent two concurrent processes from both
        acquiring the slot simultaneously.
        """
        now = self._now()
        stale_cutoff = (now - timedelta(hours=_STALE_HOURS)).isoformat()
        now_iso = now.isoformat()
        exec_id = str(uuid.uuid4())

        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Check for an active (non-stale) running execution.
            running = conn.execute(
                """SELECT id FROM schedule_executions
                   WHERE definition_id = ? AND status = 'running' AND started_at > ?
                   LIMIT 1""",
                (definition_id, stale_cutoff),
            ).fetchone()
            if running:
                conn.execute("ROLLBACK")
                return None

            # 2. run_once_per_day guard: check for completed since cutoff.
            if no_completed_since is not None:
                done = conn.execute(
                    """SELECT id FROM schedule_executions
                       WHERE definition_id = ? AND status = 'completed' AND started_at >= ?
                       LIMIT 1""",
                    (definition_id, no_completed_since.isoformat()),
                ).fetchone()
                if done:
                    conn.execute("ROLLBACK")
                    return None

            # 3. Reserve the slot.
            conn.execute(
                """INSERT INTO schedule_executions
                   (id, definition_id, trigger, status, attempt, started_at)
                   VALUES (?, ?, ?, 'running', ?, ?)""",
                (exec_id, definition_id, trigger, attempt, now_iso),
            )
            conn.execute("COMMIT")
            return exec_id

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def complete_execution(
        self,
        exec_id: str,
        run_ref: str | None = None,
        duration: float | None = None,
    ) -> None:
        self._finish(exec_id, "completed", error=None, duration=duration, run_ref=run_ref)

    def fail_execution(
        self,
        exec_id: str,
        error: str,
        duration: float | None = None,
        run_ref: str | None = None,
    ) -> None:
        """A genuine failure: counts against the day's retry budget (#68)."""
        self._finish(exec_id, "failed", error=error, duration=duration, run_ref=run_ref)

    def interrupt_execution(
        self,
        exec_id: str,
        error: str,
        duration: float | None = None,
        run_ref: str | None = None,
    ) -> None:
        """Stopped by a backend shutdown: resumable, not counted as a failure (#68)."""
        self._finish(exec_id, "interrupted", error=error, duration=duration, run_ref=run_ref)

    def skip_execution(self, exec_id: str, reason: str, duration: float | None = None) -> None:
        """Nothing was run (the source was already busy): not a failure (#68)."""
        self._finish(exec_id, "skipped", error=reason, duration=duration, run_ref=None)

    def _finish(
        self,
        exec_id: str,
        status: str,
        error: str | None,
        duration: float | None,
        run_ref: str | None,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE schedule_executions
               SET status = ?, finished_at = ?, duration_seconds = ?, error = ?, run_ref = ?
               WHERE id = ?""",
            (status, self._now().isoformat(), duration, error, run_ref, exec_id),
        )

    def get_failed_attempts_since(
        self, definition_id: str, since: datetime
    ) -> tuple[int, datetime | None]:
        """(number of 'failed' executions started since *since*, finish time of
        the latest one). Interrupted and skipped executions are not counted."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) AS n, MAX(finished_at) AS last_finished
               FROM schedule_executions
               WHERE definition_id = ? AND status = 'failed' AND started_at >= ?""",
            (definition_id, since.isoformat()),
        ).fetchone()
        last = datetime.fromisoformat(row["last_finished"]) if row["last_finished"] else None
        return int(row["n"]), last

    def recover_stale_executions(self) -> int:
        """Mark ALL running executions as interrupted on startup.

        Called once at startup, before the scheduler loop begins.  At that
        point no execution from this process can be running yet, so every
        'running' row belongs to a previous process that crashed or was killed,
        regardless of how recently it started. An interruption is resumable
        and does not count against the retry budget (#68).
        """
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE schedule_executions
               SET status      = 'interrupted',
                   finished_at  = ?,
                   error        = 'Execution interrupted: backend stopped before completion.'
               WHERE status = 'running'""",
            (self._now().isoformat(),),
        )
        count = result.rowcount
        if count:
            logger.warning("Scheduler: recovered %d stale execution(s) from a previous run.", count)
        return count


# -- helpers ------------------------------------------------------------------

def _row_to_definition(row: dict[str, Any]) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=row["id"],
        source_id=row["source_id"],
        label=row["label"],
        enabled=bool(row["enabled"]),
        startup_policy=row["startup_policy"],
        retry_max=int(row["retry_max"]),
        retry_delay_seconds=int(row["retry_delay_seconds"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
