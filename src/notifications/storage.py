"""SQLite storage for notification history.

Tracks every notification attempt (sent or failed) so the engine can:
  1. Avoid sending the same notification twice (deduplication).
  2. Report notification status in the dashboard.
  3. Allow safe retries: a 'failed' entry does not block a retry,
     but a 'sent' entry prevents re-sending on the next pipeline run.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class NotificationStorage:
    """SQLite-backed notification history for deduplication and observability."""

    # See Storage._initialized_paths in src/storage/sqlite.py: schema setup
    # only needs to run once per db file per process, not on every request.
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db") -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- connection -----------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # See src/linkedin/staging.py for why this is needed: several
            # modules open their own connection to the same jobs.db file.
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema ---------------------------------------------------------------

    def init_db(self) -> None:
        """Create the notification_history table and indexes if they do not exist.

        Migration safety: before creating the partial unique index on sent rows,
        any pre-existing duplicate (provider, event_type, entity_id) rows where
        status='sent' are collapsed to the earliest one.  This is a no-op on
        clean databases and protects against the theoretical race that could have
        produced duplicates before the unique index was introduced.
        """
        resolved = str(self._db_path.resolve())
        if resolved in NotificationStorage._initialized_paths:
            return
        conn = self._get_conn()
        # Step 1: table + non-unique indexes (idempotent)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notification_history (
                id          TEXT PRIMARY KEY,
                provider    TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                status      TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                error       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_notif_lookup
                ON notification_history(provider, event_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_notif_sent_at
                ON notification_history(sent_at DESC);
            CREATE TABLE IF NOT EXISTS department_notification_state (
                slug                            TEXT PRIMARY KEY,
                first_seen_at                   TEXT NOT NULL,
                initial_backfill_completed_at   TEXT,
                initial_backfill_sent_count     INTEGER NOT NULL DEFAULT 0
            );
        """)
        # Step 2: remove duplicate 'sent' rows (keep the earliest by rowid)
        conn.execute("""
            DELETE FROM notification_history
            WHERE status = 'sent'
              AND rowid NOT IN (
                  SELECT MIN(rowid)
                  FROM notification_history
                  WHERE status = 'sent'
                  GROUP BY provider, event_type, entity_id
              )
        """)
        conn.commit()
        # Step 3: partial unique index — now safe to create
        conn.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_sent_dedup
                ON notification_history(provider, event_type, entity_id)
                WHERE status = 'sent';
        """)
        NotificationStorage._initialized_paths.add(resolved)

    # -- deduplication --------------------------------------------------------

    def already_sent(
        self,
        provider: str,
        event_type: str,
        entity_id: str,
    ) -> bool:
        """Return True if a successful notification exists for this entity."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT id FROM notification_history
               WHERE provider = ? AND event_type = ? AND entity_id = ?
                 AND status = 'sent'
               LIMIT 1""",
            (provider, event_type, entity_id),
        ).fetchone()
        return row is not None

    # -- recording ------------------------------------------------------------

    def record(
        self,
        provider: str,
        event_type: str,
        entity_id: str,
        status: str,             # 'sent' | 'failed'
        error: str | None = None,
    ) -> None:
        """Record one notification attempt.

        For status='sent': uses INSERT OR IGNORE so the partial unique index
        (provider, event_type, entity_id WHERE status='sent') silently drops
        duplicate sent records — safe even if two threads race past already_sent().
        For status='failed': plain INSERT; multiple failure records are allowed.
        """
        conn = self._get_conn()
        verb = "INSERT OR IGNORE" if status == "sent" else "INSERT"
        conn.execute(
            f"""{verb} INTO notification_history
               (id, provider, event_type, entity_id, status, sent_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                provider,
                event_type,
                entity_id,
                status,
                datetime.now(UTC).isoformat(),
                error,
            ),
        )
        conn.commit()

    # -- queries --------------------------------------------------------------

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM notification_history ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_sent(self, provider: str | None = None) -> dict[str, Any] | None:
        """Return the most recent successfully sent notification."""
        conn = self._get_conn()
        if provider:
            row = conn.execute(
                """SELECT * FROM notification_history
                   WHERE provider = ? AND status = 'sent'
                   ORDER BY sent_at DESC LIMIT 1""",
                (provider,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM notification_history
                   WHERE status = 'sent'
                   ORDER BY sent_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM notification_history GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # -- department notification lifecycle -------------------------------------
    #
    # Tracks, per location slug, whether that department has finished
    # draining its pre-activation backlog of eligible-but-unsent jobs (see
    # NotificationEngine._route_notification_candidates' BACKFILL vs
    # UNSEEN_FOR_DEPARTMENT candidate typing). Deliberately NOT derived from
    # notification_history alone at read time ("if no prior sends: treat as
    # new") — that would misclassify a department going through a
    # legitimately quiet run (nothing eligible yet) as still "new" forever,
    # and would re-run the migration-seeding check on every single read
    # instead of once. This table makes the lifecycle an explicit, persisted
    # fact instead.

    def _has_any_sent_for_event_type(self, provider: str, event_type: str) -> bool:
        """True if ANY row (any entity_id) was successfully sent for this
        (provider, event_type) — unlike already_sent(), which checks one
        specific entity_id. Used only for migration-safe department state
        seeding below (see get_or_create_department_state)."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT id FROM notification_history
               WHERE provider = ? AND event_type = ? AND status = 'sent'
               LIMIT 1""",
            (provider, event_type),
        ).fetchone()
        return row is not None

    def get_or_create_department_state(self, slug: str) -> dict[str, Any]:
        """Return this department's lifecycle state row, creating it on
        first sight.

        Migration-safe seeding: if notification_history already has a
        successfully sent "jobs.location_batch:{slug}" row — i.e. this
        department was already sending real notifications before this
        table existed — the new row is created with
        initial_backfill_completed_at already set. An already-established
        department must never be reclassified as "newly activated" and hit
        with an initial-backfill burst of old jobs merely because this
        table is new; only a genuinely history-less slug starts in the "not
        yet backfilled" state.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM department_notification_state WHERE slug = ?", (slug,)
        ).fetchone()
        if row is not None:
            return dict(row)

        now = datetime.now(UTC).isoformat()
        has_prior_history = self._has_any_sent_for_event_type(
            "discord", f"jobs.location_batch:{slug}"
        )
        # INSERT OR IGNORE + re-SELECT: safe even if two threads/processes
        # race past the SELECT above for the same brand-new slug (same
        # pattern as record()'s 'sent' path above).
        conn.execute(
            """INSERT OR IGNORE INTO department_notification_state
               (slug, first_seen_at, initial_backfill_completed_at, initial_backfill_sent_count)
               VALUES (?, ?, ?, 0)""",
            (slug, now, now if has_prior_history else None),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM department_notification_state WHERE slug = ?", (slug,)
        ).fetchone()
        return dict(row)

    def mark_initial_backfill_complete(self, slug: str) -> None:
        """Flip a department's initial-backfill flag once its
        pre-activation backlog has been fully drained this run (see
        NotificationEngine._route_notification_candidates: the backlog is
        "drained" when a run's raw BACKFILL-type candidate count no longer
        exceeds rules.initial_backfill_max_per_department, i.e. everything
        eligible was already considered — never a naive "if no history"
        check). Idempotent: a department already marked complete keeps its
        original timestamp.
        """
        conn = self._get_conn()
        conn.execute(
            """UPDATE department_notification_state
               SET initial_backfill_completed_at = ?
               WHERE slug = ? AND initial_backfill_completed_at IS NULL""",
            (datetime.now(UTC).isoformat(), slug),
        )
        conn.commit()

    def record_backfill_sent(self, slug: str, count: int) -> None:
        """Add to the running count of BACKFILL-type jobs sent for a
        department — observability only (see stats), never read by the
        lifecycle-completion decision itself."""
        if count <= 0:
            return
        conn = self._get_conn()
        conn.execute(
            """UPDATE department_notification_state
               SET initial_backfill_sent_count = initial_backfill_sent_count + ?
               WHERE slug = ?""",
            (count, slug),
        )
        conn.commit()
