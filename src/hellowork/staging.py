"""Persistent HelloWork staging storage.

Mirrors src/linkedin/staging.py's design (see that module's docstring for the
full rationale) so that HelloWork's ~30-minute collection runs decoupled from
the main pipeline run, the same way LinkedIn's does:

  waiting      — discovered by the scanner, ready for the next pipeline run
  snapshotted  — locked into a specific pipeline run (snapshot_run_id is set)
  transferred  — pipeline completed successfully; job is now in the jobs table
  processed    — pipeline ran; job was filtered/deduplicated/invalid, not stored

State transitions:

  [scanner inserts]          ->  waiting
  snapshot_waiting(run_id)   :   waiting      -> snapshotted   (pipeline starts)
  mark_transferred_by_...    :   snapshotted  -> transferred   (job stored)
  mark_remaining_as_processed:   snapshotted  -> processed     (job not stored)
  reset_snapshot(run_id)     :   snapshotted  -> waiting       (pipeline failure)

Simpler than LinkedIn's staging in one respect: HelloWork's collector fetches
each job's full JobPosting JSON-LD in one pass (title/company/location/
description all at once), so there is no "partial record, description
fetched later" state to track -- a staged record is always complete.
Records are never deleted; re-discovery of an already-terminal (transferred/
processed) job only refreshes last_seen_at, so a job is never reprocessed
indefinitely once the pipeline has already decided its fate.

Thread safety: identical to LinkedIn's -- the scanner holds its own
_scan_lock while scanning (one writer at a time), PipelineRunner._lock
prevents concurrent snapshot_waiting() calls, and SQLite WAL mode lets the
scanner insert rows while the pipeline reads snapshotted rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.scan_control import (
    CONFIG_CHANGED_RUN_NOTE,
    RESUMABLE_RUN_STATUSES,
    SHUTDOWN_RUN_NOTE,
    add_checkpoint_column,
    mark_run,
    recover_stale_runs,
    reopen_run,
)

if TYPE_CHECKING:
    from src.models.job import Job

logger = logging.getLogger(__name__)


class HelloWorkStagingStorage:
    """SQLite storage for HelloWork staging records and scan run metadata."""

    # See Storage._initialized_paths in src/storage/sqlite.py / the matching
    # guard in src/linkedin/staging.py: a fresh instance is constructed and
    # init_db()'d on every pipeline run (src/api/staged_sources.py), so this
    # avoids an extra write transaction against hellowork_staging on every
    # run once the schema already exists.
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db") -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- connection -----------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
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
        resolved = str(self._db_path.resolve())
        if resolved in HelloWorkStagingStorage._initialized_paths:
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hellowork_staging (
                id                  TEXT PRIMARY KEY,
                source_job_id       TEXT NOT NULL UNIQUE,
                canonical_url       TEXT NOT NULL UNIQUE,
                title               TEXT,
                company             TEXT,
                location            TEXT,
                countries           TEXT NOT NULL DEFAULT '[]',
                is_remote           INTEGER NOT NULL DEFAULT 0,
                work_mode           TEXT,
                employment_type     TEXT,
                salary_min          REAL,
                salary_max          REAL,
                salary_currency     TEXT NOT NULL DEFAULT 'EUR',
                description         TEXT,
                published_at        TEXT,
                raw_payload         TEXT NOT NULL DEFAULT '{}',

                status              TEXT NOT NULL DEFAULT 'waiting',
                snapshot_run_id     TEXT,
                scan_run_id         TEXT NOT NULL,
                discovered_at       TEXT NOT NULL,
                last_seen_at        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hellowork_staging_status
                ON hellowork_staging(status);
            CREATE INDEX IF NOT EXISTS idx_hellowork_staging_snapshot_run_id
                ON hellowork_staging(snapshot_run_id);

            CREATE TABLE IF NOT EXISTS hellowork_scan_runs (
                id           TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                completed_at TEXT,
                status       TEXT NOT NULL DEFAULT 'running',
                jobs_found   INTEGER NOT NULL DEFAULT 0,
                jobs_new     INTEGER NOT NULL DEFAULT 0,
                stats_json   TEXT NOT NULL DEFAULT '{}',
                error        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hellowork_scan_runs_started_at
                ON hellowork_scan_runs(started_at DESC);

            -- Resumable scan checkpoint: a single row (id=1) tracking which
            -- metier slugs have been fully processed (listing + all detail
            -- pages attempted) in the current, possibly-interrupted scan
            -- attempt. Written only by the scanner thread under its own
            -- _scan_lock, one small UPDATE at a time -- never touched by API
            -- routes or dashboard polling, so normal app usage cannot reset
            -- it. Cleared when a scan completes all slugs successfully, so
            -- the next *scheduled* scan starts fresh (this table only exists
            -- to survive an interruption, not to skip legitimate re-scans).
            CREATE TABLE IF NOT EXISTS hellowork_scan_checkpoint (
                id                   INTEGER PRIMARY KEY CHECK (id = 1),
                scan_id              TEXT NOT NULL,
                completed_slugs_json TEXT NOT NULL DEFAULT '[]',
                updated_at           TEXT NOT NULL
            );
        """)
        conn.commit()
        # Pause/resume (issue #81): lifecycle state of the checkpointed scan
        # (running|paused|interrupted|failed; NULL = legacy row, treated as
        # interrupted) and the config it was taken under.
        existing = {
            r["name"] for r in conn.execute("PRAGMA table_info(hellowork_scan_checkpoint)")
        }
        for column in ("state", "config_fingerprint"):
            if column not in existing:
                add_checkpoint_column(conn, "hellowork_scan_checkpoint", column)
        conn.commit()
        HelloWorkStagingStorage._initialized_paths.add(resolved)

    # -- hellowork_staging CRUD ------------------------------------------------

    def insert_job(self, record: dict[str, Any]) -> bool:
        """Insert a job or refresh it if already known.

        Returns True when the job is new (first time seen), False otherwise.

        Rows already in a terminal state ('transferred' or 'processed') only
        get last_seen_at bumped -- their content and status are left alone,
        so a job the pipeline already decided on is never silently re-queued
        just because a later scan re-discovers the same still-live posting.
        Rows in 'waiting' or 'snapshotted' get their content refreshed (the
        listing may have changed slightly between scans).
        """
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id FROM hellowork_staging WHERE id = ?", (record["id"],)
        ).fetchone()
        is_new = existing is None

        conn.execute(
            """INSERT INTO hellowork_staging
               (id, source_job_id, canonical_url, title, company, location,
                countries, is_remote, work_mode, employment_type,
                salary_min, salary_max, salary_currency,
                description, published_at, raw_payload,
                scan_run_id, discovered_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   title = CASE WHEN status IN ('waiting','snapshotted') THEN excluded.title ELSE title END,
                   company = CASE WHEN status IN ('waiting','snapshotted') THEN excluded.company ELSE company END,
                   location = CASE WHEN status IN ('waiting','snapshotted') THEN excluded.location ELSE location END,
                   description = CASE WHEN status IN ('waiting','snapshotted') THEN excluded.description ELSE description END,
                   raw_payload = CASE WHEN status IN ('waiting','snapshotted') THEN excluded.raw_payload ELSE raw_payload END""",
            (
                record["id"],
                record["source_job_id"],
                record["canonical_url"],
                record.get("title"),
                record.get("company"),
                record.get("location"),
                record.get("countries", "[]"),
                record.get("is_remote", 0),
                record.get("work_mode"),
                record.get("employment_type"),
                record.get("salary_min"),
                record.get("salary_max"),
                record.get("salary_currency", "EUR"),
                record.get("description"),
                record.get("published_at"),
                record.get("raw_payload", "{}"),
                record["scan_run_id"],
                record["discovered_at"],
                record["last_seen_at"],
            ),
        )
        conn.commit()
        return is_new

    def snapshot_waiting(self, run_id: str) -> list[dict[str, Any]]:
        """Atomically lock all waiting jobs into a pipeline run snapshot.

        Identical mechanism to LinkedInStagingStorage.snapshot_waiting: a
        single atomic UPDATE, then a SELECT of the just-locked rows.
        """
        conn = self._get_conn()
        conn.execute(
            """UPDATE hellowork_staging
               SET status = 'snapshotted', snapshot_run_id = ?
               WHERE status = 'waiting' AND snapshot_run_id IS NULL""",
            (run_id,),
        )
        conn.commit()
        rows = conn.execute(
            """SELECT * FROM hellowork_staging
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_transferred_by_stable_ids(self, run_id: str, stable_ids: set[str]) -> int:
        """Mark only the specified jobs as 'transferred' after a successful pipeline run."""
        if not stable_ids:
            return 0
        conn = self._get_conn()
        placeholders = ",".join("?" * len(stable_ids))
        result = conn.execute(
            f"""UPDATE hellowork_staging SET status = 'transferred'
                WHERE snapshot_run_id = ?
                  AND status = 'snapshotted'
                  AND id IN ({placeholders})""",
            (run_id, *stable_ids),
        )
        conn.commit()
        return result.rowcount

    def mark_remaining_as_processed(self, run_id: str) -> int:
        """Mark any still-snapshotted records as 'processed' after a successful pipeline run."""
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE hellowork_staging SET status = 'processed'
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        )
        conn.commit()
        return result.rowcount

    def reset_snapshot(self, run_id: str) -> int:
        """Reset snapshotted jobs back to waiting after a failed pipeline run."""
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE hellowork_staging
               SET status = 'waiting', snapshot_run_id = NULL
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        )
        conn.commit()
        count = result.rowcount
        if count:
            logger.info("HelloWork: reset %d snapshotted job(s) to waiting (run %s failed)", count, run_id)
        return count

    # -- startup recovery -------------------------------------------------------

    def recover_stale_scans(self) -> int:
        """Relabel scan runs left 'running' by a previous process at startup.

        The run the checkpoint belongs to becomes 'interrupted' (it resumes
        as the same logical scan, issue #86); any other stale run has no
        progress to resume and is marked 'failed' as before.
        """
        conn = self._get_conn()
        count = recover_stale_runs(conn, "hellowork_scan_runs", "hellowork_scan_checkpoint")
        # A checkpoint still marked 'running' belongs to a scan whose process
        # died without a cooperative stop: its last saved slug is the durable
        # resume point, resumed automatically (issue #81). 'paused' is left
        # alone — a user pause survives restarts.
        cp = conn.execute(
            """UPDATE hellowork_scan_checkpoint SET state = 'interrupted'
               WHERE state = 'running'"""
        )
        conn.commit()
        if count:
            logger.warning("HelloWork: recovered %d stale scan run(s) from a previous process.", count)
        if cp.rowcount:
            logger.warning(
                "HelloWork: scan interrupted by a process stop; its checkpoint will be resumed."
            )
        return count

    def reset_stale_snapshots(self) -> int:
        """Reset any 'snapshotted' jobs to 'waiting' on application startup."""
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE hellowork_staging
               SET status = 'waiting', snapshot_run_id = NULL
               WHERE status = 'snapshotted'"""
        )
        conn.commit()
        count = result.rowcount
        if count:
            logger.info("HelloWork: reset %d stale snapshotted job(s) to waiting.", count)
        return count

    # -- hellowork_scan_runs CRUD ------------------------------------------------

    def create_scan_run(self, scan_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO hellowork_scan_runs (id, started_at, status)
               VALUES (?, ?, 'running')""",
            (scan_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()

    def complete_scan_run(self, scan_id: str, jobs_found: int, jobs_new: int, stats_json: str = "{}") -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE hellowork_scan_runs
               SET status = 'completed', completed_at = ?,
                   jobs_found = ?, jobs_new = ?, stats_json = ?
               WHERE id = ?""",
            (datetime.now(UTC).isoformat(), jobs_found, jobs_new, stats_json, scan_id),
        )
        conn.commit()

    def fail_scan_run(self, scan_id: str, error: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE hellowork_scan_runs
               SET status = 'failed', completed_at = ?, error = ?
               WHERE id = ?""",
            (datetime.now(UTC).isoformat(), error[:2000], scan_id),
        )
        conn.commit()

    def pause_scan_run(self, scan_id: str) -> None:
        """Record a scan stopped at a checkpoint boundary by a user pause.

        'paused' is not touched by recover_stale_scans(), so it survives a
        restart; resuming reopens this same run (issue #86).
        """
        conn = self._get_conn()
        conn.execute(
            """UPDATE hellowork_scan_runs
               SET status = 'paused', completed_at = ?, error = NULL
               WHERE id = ?""",
            (datetime.now(UTC).isoformat(), scan_id),
        )
        conn.commit()

    def interrupt_scan_run(self, scan_id: str) -> None:
        """Record a running scan stopped for backend shutdown: resumable, not failed."""
        mark_run(self._get_conn(), "hellowork_scan_runs", scan_id, "interrupted",
                 SHUTDOWN_RUN_NOTE, ("running",))

    def reopen_scan_run(self, scan_id: str) -> bool:
        """Reopen an interrupted/paused run as the same logical scan (issue #86).

        Keeps started_at and the jobs_found/jobs_new accumulated by earlier
        attempts; clears completed_at/error. False when no such resumable
        row exists (the caller then records a new run).
        """
        return reopen_run(self._get_conn(), "hellowork_scan_runs", scan_id)

    def abandon_scan_run(self, scan_id: str) -> None:
        """Close an interrupted/paused run whose checkpoint was discarded
        because the config changed, so it does not stay 'interrupted'."""
        mark_run(self._get_conn(), "hellowork_scan_runs", scan_id, "failed",
                 CONFIG_CHANGED_RUN_NOTE, RESUMABLE_RUN_STATUSES)

    def get_scan_run(self, scan_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM hellowork_scan_runs WHERE id = ?", (scan_id,)).fetchone()
        return dict(row) if row else None

    def list_scan_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM hellowork_scan_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- resumable scan checkpoint ----------------------------------------------
    #
    # Single-row table: at most one HelloWork scan can be running at a time
    # (HelloWorkScanner._scan_lock), so there is only ever one checkpoint to
    # track. See src/hellowork/scanner.py::_do_scan for how this is used.

    def get_scan_checkpoint(self) -> dict[str, Any] | None:
        """Return the current checkpoint row, or None if there isn't one.

        No checkpoint means either no scan has ever run, or the last scan
        that touched it completed successfully (which clears it).
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM hellowork_scan_checkpoint WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def save_scan_checkpoint(
        self,
        scan_id: str,
        completed_slugs: list[str],
        state: str = "running",
        fingerprint: str | None = None,
        jobs_found: int | None = None,
        jobs_new: int | None = None,
    ) -> None:
        """Persist which metier slugs are fully done so far in this scan.

        A single UPDATE-or-INSERT of the singleton row, committed
        immediately — the write is small and fast, so it does not hold the
        writer lock long enough to meaningfully contend with concurrent
        activity.

        ``fingerprint`` identifies the scanner config the progress was made
        under; None keeps the stored one (issue #81).

        ``jobs_found``/``jobs_new`` are the logical scan's running totals,
        written to its run row in the same transaction so a resumed attempt
        continues from exactly the counts matching the checkpoint (#86).
        """
        conn = self._get_conn()
        if jobs_found is not None and jobs_new is not None:
            conn.execute(
                "UPDATE hellowork_scan_runs SET jobs_found = ?, jobs_new = ? WHERE id = ?",
                (jobs_found, jobs_new, scan_id),
            )
        conn.execute(
            """INSERT INTO hellowork_scan_checkpoint
                   (id, scan_id, completed_slugs_json, updated_at, state, config_fingerprint)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   scan_id = excluded.scan_id,
                   completed_slugs_json = excluded.completed_slugs_json,
                   updated_at = excluded.updated_at,
                   state = excluded.state,
                   config_fingerprint = COALESCE(excluded.config_fingerprint, config_fingerprint)""",
            (scan_id, json.dumps(completed_slugs), datetime.now(UTC).isoformat(), state, fingerprint),
        )
        conn.commit()

    def set_checkpoint_state(
        self, scan_id: str, state: str, create_if_missing: bool = True
    ) -> None:
        """Set the checkpoint's lifecycle state without touching its progress.

        Creates an empty-progress row if none exists yet (a pause before the
        first slug completed resumes from the beginning). Written only by the
        scanner thread (issue #81).

        With ``create_if_missing=False`` only an existing row is updated (a
        shutdown/failure before any progress leaves no checkpoint behind).
        """
        conn = self._get_conn()
        if not create_if_missing:
            conn.execute(
                "UPDATE hellowork_scan_checkpoint SET state = ?, scan_id = ?, updated_at = ? WHERE id = 1",
                (state, scan_id, datetime.now(UTC).isoformat()),
            )
            conn.commit()
            return
        conn.execute(
            """INSERT INTO hellowork_scan_checkpoint (id, scan_id, updated_at, state)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   state = excluded.state,
                   updated_at = excluded.updated_at""",
            (scan_id, datetime.now(UTC).isoformat(), state),
        )
        conn.commit()

    def clear_scan_checkpoint(self) -> None:
        """Remove the checkpoint — called once a scan completes all slugs.

        A scan that finishes cleanly has nothing left to resume; the next
        *scheduled* scan should start from the first slug again (that is
        the normal, intended way to pick up newly-posted jobs), not skip
        everything because of a stale checkpoint.
        """
        conn = self._get_conn()
        conn.execute("DELETE FROM hellowork_scan_checkpoint WHERE id = 1")
        conn.commit()

    # -- status query -----------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return a status snapshot: waiting count and last scan metadata."""
        conn = self._get_conn()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM hellowork_staging WHERE status = 'waiting'"
        ).fetchone()[0]
        last_row = conn.execute(
            "SELECT * FROM hellowork_scan_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return {
            "waiting": waiting,
            "last_scan": dict(last_row) if last_row else None,
        }

    def count_by_status(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM hellowork_staging GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# -- conversion ---------------------------------------------------------------

def job_to_staging_record(job: "Job", scan_run_id: str) -> dict[str, Any]:
    """Convert a normalized Job (source='hellowork') into a staging record dict."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": job.id,
        "source_job_id": job.source_job_id or job.id,
        "canonical_url": job.canonical_url or f"urn:hellowork:{job.id}",
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "countries": json.dumps(job.countries or []),
        "is_remote": int(bool(job.remote)),
        "work_mode": job.work_mode,
        "employment_type": job.employment_type,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency or "EUR",
        "description": job.description,
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "raw_payload": json.dumps(job.raw_payload or {}),
        "scan_run_id": scan_run_id,
        "discovered_at": now,
        "last_seen_at": now,
    }


def staging_row_to_job(row: dict[str, Any]) -> "Job":
    """Convert a hellowork_staging row back into a pipeline-ready Job object."""
    from src.models.job import Job
    from src.pipeline.normalize import normalize_country_code

    published_at = None
    raw_pub = row.get("published_at")
    if raw_pub:
        try:
            published_at = datetime.fromisoformat(str(raw_pub))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass

    discovered_at = datetime.now(UTC)
    raw_disc = row.get("discovered_at")
    if raw_disc:
        try:
            discovered_at = datetime.fromisoformat(str(raw_disc))
            if discovered_at.tzinfo is None:
                discovered_at = discovered_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass

    raw_countries: list[str] = json.loads(row.get("countries") or "[]")
    countries: list[str] = [normalize_country_code(c) for c in raw_countries]
    raw_payload: dict[str, Any] = json.loads(row.get("raw_payload") or "{}")
    title = row.get("title") or ""
    description = row.get("description") or ""

    content_hash_source = title + " " + description
    import hashlib
    content_hash = hashlib.md5(content_hash_source.encode()).hexdigest()

    return Job(
        id=row["id"],
        source="hellowork",
        source_job_id=row.get("source_job_id"),
        canonical_url=row.get("canonical_url"),
        apply_url=row.get("canonical_url"),
        company=row.get("company") or None,
        title=title.strip() or None,
        description=description.strip() or None,
        location=row.get("location") or None,
        countries=countries,
        remote=bool(row.get("is_remote", False)),
        work_mode=row.get("work_mode") or None,
        employment_type=row.get("employment_type") or None,
        salary_min=row.get("salary_min"),
        salary_max=row.get("salary_max"),
        salary_currency=row.get("salary_currency") or "EUR",
        published_at=published_at,
        discovered_at=discovered_at,
        raw_payload=raw_payload,
        content_hash=content_hash,
    )
