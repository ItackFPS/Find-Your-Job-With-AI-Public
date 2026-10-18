"""Persistent LinkedIn staging storage.

LinkedIn jobs flow through these states:

  waiting      — discovered by the scanner, ready for the next pipeline run
  snapshotted  — locked into a specific pipeline run (snapshot_run_id is set)
  transferred  — pipeline completed successfully; job is now in the jobs table

State transitions:

  [scanner inserts]          →  waiting
  snapshot_waiting(run_id)   :  waiting      → snapshotted   (pipeline starts)
  mark_transferred(run_id)   :  snapshotted  → transferred   (pipeline success)
  reset_snapshot(run_id)     :  snapshotted  → waiting       (pipeline failure)

Records are never deleted.  Raw payload, LinkedIn job ID, canonical URL,
discovery time, and last-seen time are preserved permanently.

Thread safety:
  - The scanner holds its own _scan_lock while scanning, so only one writer
    ever calls insert_job() at a time.
  - PipelineRunner._lock prevents two pipeline runs from calling
    snapshot_waiting() concurrently.
  - SQLite WAL mode allows the scanner to insert rows while the pipeline reads
    snapshotted rows — the two operations target disjoint row sets.
"""

from __future__ import annotations

import hashlib
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


class LinkedInStagingStorage:
    """SQLite storage for LinkedIn staging records and scan run metadata."""

    # See Storage._initialized_paths in src/storage/sqlite.py for the full
    # rationale. This class's init_db() additionally runs full-table UPDATE
    # backfills (salary_currency, work_mode) on every call — a fresh instance
    # is constructed and init_db()'d on every pipeline run (see
    # src/api/staged_sources.py::LinkedInStagedSourceAdapter.snapshot_for_run),
    # so without this guard every pipeline run re-scanned the whole
    # linkedin_staging table under a write transaction, contending with the
    # scanner's concurrent per-row inserts and surfacing as "database is
    # locked".
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
            # Wait for a concurrent writer to finish instead of raising
            # "database is locked" immediately (default busy_timeout is 0).
            # Several modules (pipeline, scheduler, notifications) open their
            # own connection to this same jobs.db file.
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema ---------------------------------------------------------------

    def init_db(self) -> None:
        resolved = str(self._db_path.resolve())
        if resolved in LinkedInStagingStorage._initialized_paths:
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS linkedin_staging (
                id                  TEXT PRIMARY KEY,
                linkedin_job_id     TEXT NOT NULL UNIQUE,
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
                salary_currency     TEXT NOT NULL DEFAULT 'USD',
                description         TEXT,
                description_fetched INTEGER NOT NULL DEFAULT 0,
                published_at        TEXT,
                raw_payload         TEXT NOT NULL DEFAULT '{}',

                status              TEXT NOT NULL DEFAULT 'waiting',
                snapshot_run_id     TEXT,
                scan_run_id         TEXT NOT NULL,
                discovered_at       TEXT NOT NULL,
                last_seen_at        TEXT NOT NULL,

                -- Query attribution (populated when scanner runs with attribution enabled)
                discovered_by_query TEXT,
                seen_by_queries     TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_linkedin_staging_status
                ON linkedin_staging(status);
            CREATE INDEX IF NOT EXISTS idx_linkedin_staging_snapshot_run_id
                ON linkedin_staging(snapshot_run_id);
            CREATE INDEX IF NOT EXISTS idx_linkedin_staging_discovered_at
                ON linkedin_staging(discovered_at DESC);

            CREATE TABLE IF NOT EXISTS linkedin_scan_runs (
                id           TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                completed_at TEXT,
                status       TEXT NOT NULL DEFAULT 'running',
                queries_json TEXT NOT NULL DEFAULT '[]',
                jobs_found   INTEGER NOT NULL DEFAULT 0,
                jobs_new     INTEGER NOT NULL DEFAULT 0,
                error        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_linkedin_scan_runs_started_at
                ON linkedin_scan_runs(started_at DESC);

            -- Resumable scan checkpoint: a single row (id=1) tracking which
            -- queries are fully done and which page the in-progress query
            -- had reached, for the current, possibly-interrupted scan
            -- attempt. Written only by the scanner thread under its own
            -- _scan_lock, one small UPDATE at a time -- never touched by API
            -- routes or dashboard polling, so normal app usage cannot reset
            -- it. Cleared when a scan completes every query successfully,
            -- so the next *scheduled* scan starts from page 0 again (this
            -- table only exists to survive an interruption, not to skip
            -- legitimate re-scans).
            CREATE TABLE IF NOT EXISTS linkedin_scan_checkpoint (
                id                     INTEGER PRIMARY KEY CHECK (id = 1),
                scan_id                TEXT NOT NULL,
                completed_queries_json TEXT NOT NULL DEFAULT '[]',
                current_query_key      TEXT,
                current_page           INTEGER NOT NULL DEFAULT 0,
                updated_at             TEXT NOT NULL
            );
        """)
        conn.commit()
        # Schema migrations: add columns that may not exist in older databases.
        for migration in (
            "ALTER TABLE linkedin_scan_runs ADD COLUMN stats_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE linkedin_staging ADD COLUMN discovered_by_query TEXT",
            "ALTER TABLE linkedin_staging ADD COLUMN seen_by_queries TEXT NOT NULL DEFAULT '[]'",
        ):
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        # Pause/resume (issue #81): lifecycle state of the checkpointed scan
        # (running|paused|interrupted|failed; NULL = legacy row, treated as
        # interrupted) and the config it was taken under. stats_json (issue
        # #86): the logical scan's running statistics — per-query counters
        # and active processing seconds — so a scan spanning several
        # processes reports every attempt; NULL on rows written before it.
        for column in ("state", "config_fingerprint", "stats_json"):
            add_checkpoint_column(conn, "linkedin_scan_checkpoint", column)
        conn.commit()

        # Data migration: fix historical rows where salary_currency was set to
        # "USD" (the previous default) for jobs that have no actual salary data.
        # These rows were created before the EUR default was introduced.
        # Conservative: only touch rows with no salary_min AND no salary_max.
        try:
            conn.execute(
                """UPDATE linkedin_staging
                   SET salary_currency = 'EUR'
                   WHERE salary_currency = 'USD'
                     AND salary_min IS NULL
                     AND salary_max IS NULL"""
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # Data migration: backfill work_mode for waiting records where the
        # location string carries an unambiguous signal.  Pure-SQL is safe
        # here because the patterns match only explicit markers.  Title-based
        # work_mode backfill requires Python and is in scripts/backfill_staging.py.
        for wm_sql in (
            # LinkedIn location "Remote" (exact, case-insensitive)
            """UPDATE linkedin_staging SET work_mode='remote', is_remote=1
               WHERE work_mode IS NULL AND status='waiting'
                 AND LOWER(TRIM(location))='remote'""",
            # Location suffix "(Remote)" or "(Télétravail)"
            """UPDATE linkedin_staging SET work_mode='remote', is_remote=1
               WHERE work_mode IS NULL AND status='waiting'
                 AND (LOWER(location) LIKE '%(remote)%'
                      OR LOWER(location) LIKE '%(télétravail)%'
                      OR LOWER(location) LIKE '%(teletravail)%')""",
            # Location suffix "(Hybrid)" or "(Hybride)"
            """UPDATE linkedin_staging SET work_mode='hybrid'
               WHERE work_mode IS NULL AND status='waiting'
                 AND (LOWER(location) LIKE '%(hybrid)%'
                      OR LOWER(location) LIKE '%(hybride)%')""",
        ):
            try:
                conn.execute(wm_sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass

        LinkedInStagingStorage._initialized_paths.add(resolved)

    # -- linkedin_staging CRUD -----------------------------------------------

    def insert_job(self, record: dict[str, Any]) -> bool:
        """Insert a job or update last_seen_at / description if already known.

        Returns True when the job is new (first time seen), False when it
        already existed in staging.

        On re-discovery of a job whose description was not previously fetched,
        the description and description_fetched flag are updated if the new
        record carries a non-empty description.  An already-complete
        description is never overwritten.

        If the record instead carries description_fetched=2 (the scanner's
        targeted fetch classified the job as HTTP 404/410 — permanently
        gone), a job still at description_fetched=0 transitions to 2 so
        needs_description() stops retrying it.  This never touches status/
        snapshot_run_id — a deleted job is not reopened, only marked
        non-retryable for future description fetches.

        If that same re-discovery newly completes the description for a job
        stuck in a terminal state ('transferred' or 'processed' — i.e. a
        stale record from before description enrichment worked), the status
        is reset to 'waiting' (and snapshot_run_id cleared) so the next
        pipeline run picks it up and upserts the now-complete job into the
        main jobs table.  'waiting'/'snapshotted' rows are left untouched —
        they are already queued or actively being processed.

        The scanner lock ensures only one writer calls this at a time, so the
        SELECT followed by the INSERT/UPDATE is race-free.
        """
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id FROM linkedin_staging WHERE linkedin_job_id = ?",
            (record["linkedin_job_id"],),
        ).fetchone()
        is_new = existing is None

        discovered_by_query = record.get("discovered_by_query")
        seen_by_queries = (
            json.dumps([discovered_by_query]) if discovered_by_query else "[]"
        )

        conn.execute(
            """INSERT INTO linkedin_staging
               (id, linkedin_job_id, canonical_url, title, company, location,
                countries, is_remote, work_mode, employment_type,
                salary_min, salary_max, salary_currency,
                description, description_fetched, published_at,
                raw_payload, scan_run_id, discovered_at, last_seen_at,
                discovered_by_query, seen_by_queries)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(linkedin_job_id) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   countries = CASE
                       WHEN (countries IS NULL OR countries = '[]')
                            AND excluded.countries IS NOT NULL
                            AND excluded.countries != '[]'
                       THEN excluded.countries
                       ELSE countries
                   END,
                   description = CASE
                       WHEN description_fetched = 0
                            AND excluded.description IS NOT NULL
                            AND excluded.description != ''
                       THEN excluded.description
                       ELSE description
                   END,
                   description_fetched = CASE
                       WHEN description_fetched = 0
                            AND excluded.description IS NOT NULL
                            AND excluded.description != ''
                       THEN 1
                       WHEN description_fetched = 0
                            AND excluded.description_fetched = 2
                       THEN 2
                       ELSE description_fetched
                   END,
                   status = CASE
                       WHEN description_fetched = 0
                            AND excluded.description IS NOT NULL
                            AND excluded.description != ''
                            AND status IN ('transferred', 'processed')
                       THEN 'waiting'
                       ELSE status
                   END,
                   snapshot_run_id = CASE
                       WHEN description_fetched = 0
                            AND excluded.description IS NOT NULL
                            AND excluded.description != ''
                            AND status IN ('transferred', 'processed')
                       THEN NULL
                       ELSE snapshot_run_id
                   END""",
            (
                record["id"],
                record["linkedin_job_id"],
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
                record.get("description_fetched", 0),
                record.get("published_at"),
                record.get("raw_payload", "{}"),
                record["scan_run_id"],
                record["discovered_at"],
                record["last_seen_at"],
                discovered_by_query,
                seen_by_queries,
            ),
        )
        conn.commit()
        return is_new

    def needs_description(self, linkedin_job_id: str) -> bool:
        """Return True if this job should have its description (re-)fetched.

        True when the job is unseen, or seen but its description was never
        successfully fetched (description_fetched=0) — this covers stale
        records collected under previously-broken description enrichment,
        regardless of their pipeline status (waiting/transferred/processed),
        so they are not permanently blocked from rediscovery.

        False when a description was already fetched (description_fetched=1)
        or the job is permanently gone (description_fetched=2, HTTP 404/410)
        and must not be retried.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT description_fetched FROM linkedin_staging WHERE linkedin_job_id = ?",
            (linkedin_job_id,),
        ).fetchone()
        if row is None:
            return True
        not_fetched: bool = row["description_fetched"] == 0
        return not_fetched

    def snapshot_waiting(self, run_id: str) -> list[dict[str, Any]]:
        """Atomically lock all waiting jobs into a pipeline run snapshot.

        A single UPDATE statement is used, which is atomic in SQLite.  After
        this call:
          - All previously 'waiting' jobs have status='snapshotted' and
            snapshot_run_id = run_id.
          - Jobs inserted by the scanner after this call are NOT included.
          - Returns the snapshotted rows ready for pipeline processing.

        PipelineRunner._lock prevents two concurrent pipeline runs from
        calling this simultaneously, so no additional locking is needed here.
        """
        conn = self._get_conn()
        conn.execute(
            """UPDATE linkedin_staging
               SET status = 'snapshotted', snapshot_run_id = ?
               WHERE status = 'waiting' AND snapshot_run_id IS NULL""",
            (run_id,),
        )
        conn.commit()
        rows = conn.execute(
            """SELECT * FROM linkedin_staging
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_transferred(self, run_id: str) -> int:
        """Mark ALL snapshotted jobs as transferred after a successful pipeline run.

        This is the legacy method — it marks every snapshotted record regardless
        of whether the individual job was stored in the jobs table.  Prefer
        ``mark_transferred_by_stable_ids`` + ``mark_remaining_as_processed``
        when the set of stored stable IDs is available.
        """
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE linkedin_staging SET status = 'transferred'
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        )
        conn.commit()
        return result.rowcount

    def mark_transferred_by_stable_ids(
        self, run_id: str, stable_ids: set[str]
    ) -> int:
        """Mark only the specified jobs as 'transferred' after a successful pipeline run.

        ``stable_ids`` is the set of stable IDs (sha256[:32] of
        "linkedin:<linkedin_job_id>") that actually made it into the jobs table
        (i.e., survived validation, deduplication, and the hard filter).

        Jobs in this snapshot that are NOT in ``stable_ids`` should be marked
        with ``mark_remaining_as_processed`` to maintain a consistent terminal
        state for every staged record.
        """
        if not stable_ids:
            return 0
        conn = self._get_conn()
        placeholders = ",".join("?" * len(stable_ids))
        result = conn.execute(
            f"""UPDATE linkedin_staging SET status = 'transferred'
                WHERE snapshot_run_id = ?
                  AND status = 'snapshotted'
                  AND id IN ({placeholders})""",
            (run_id, *stable_ids),
        )
        conn.commit()
        return result.rowcount

    def mark_remaining_as_processed(self, run_id: str) -> int:
        """Mark any still-snapshotted records as 'processed' after a successful pipeline run.

        'processed' means the pipeline ran on this batch and succeeded, but this
        specific job was not stored in the jobs table — it was filtered out,
        deduplicated, or failed validation.  This status satisfies the invariant:

            transferred → job is in the jobs table
            processed   → pipeline ran; job was not stored (filtered/deduped/invalid)

        Call this AFTER ``mark_transferred_by_stable_ids`` so that only the
        non-transferred subset receives the 'processed' status.
        """
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE linkedin_staging SET status = 'processed'
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        )
        conn.commit()
        count = result.rowcount
        if count:
            logger.debug(
                "LinkedIn: marked %d job(s) as 'processed' (pipeline ran; not stored) "
                "for run %s.",
                count,
                run_id,
            )
        return count

    def reset_snapshot(self, run_id: str) -> int:
        """Reset snapshotted jobs back to waiting after a failed pipeline run.

        This ensures a pipeline crash never permanently removes jobs from the
        waiting queue.  Returns the number of rows reset.
        """
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE linkedin_staging
               SET status = 'waiting', snapshot_run_id = NULL
               WHERE snapshot_run_id = ? AND status = 'snapshotted'""",
            (run_id,),
        )
        conn.commit()
        count = result.rowcount
        if count:
            logger.info("LinkedIn: reset %d snapshotted job(s) to waiting (run %s failed)", count, run_id)
        return count

    # -- startup recovery -----------------------------------------------------

    def recover_stale_scans(self) -> int:
        """Relabel scan runs left 'running' by a previous process at startup.

        The run the checkpoint belongs to becomes 'interrupted' (it resumes
        as the same logical scan, issue #86); any other stale run has no
        progress to resume and is marked 'failed' as before. Called during
        the FastAPI lifespan startup hook, before any threads are started,
        so no genuinely active scan can be affected.
        """
        conn = self._get_conn()
        count = recover_stale_runs(conn, "linkedin_scan_runs", "linkedin_scan_checkpoint")
        # A checkpoint still marked 'running' belongs to a scan whose process
        # died without a cooperative stop (crash, kill, power loss): its last
        # saved page is the durable resume point, and it is resumed
        # automatically like a shutdown stop (issue #81). 'paused' is left
        # alone — a user pause survives restarts.
        cp = conn.execute(
            """UPDATE linkedin_scan_checkpoint SET state = 'interrupted'
               WHERE state = 'running'"""
        )
        conn.commit()
        if count:
            logger.warning("LinkedIn: recovered %d stale scan run(s) from a previous process.", count)
        if cp.rowcount:
            logger.warning(
                "LinkedIn: scan interrupted by a process stop; its checkpoint will be resumed."
            )
        return count

    def reset_stale_snapshots(self) -> int:
        """Reset any 'snapshotted' jobs to 'waiting' on application startup.

        Called after recover_stale_runs() in the pipeline RunStorage, so that
        any job snapshotted by a now-failed pipeline run is safely returned to
        the waiting queue.
        """
        conn = self._get_conn()
        result = conn.execute(
            """UPDATE linkedin_staging
               SET status = 'waiting', snapshot_run_id = NULL
               WHERE status = 'snapshotted'"""
        )
        conn.commit()
        count = result.rowcount
        if count:
            logger.info("LinkedIn: reset %d stale snapshotted job(s) to waiting.", count)
        return count

    # -- linkedin_scan_runs CRUD ----------------------------------------------

    def create_scan_run(self, scan_id: str, queries_json: str = "[]") -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO linkedin_scan_runs (id, started_at, status, queries_json)
               VALUES (?, ?, 'running', ?)""",
            (scan_id, datetime.now(UTC).isoformat(), queries_json),
        )
        conn.commit()

    def complete_scan_run(
        self,
        scan_id: str,
        jobs_found: int,
        jobs_new: int,
        stats_json: str = "{}",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE linkedin_scan_runs
               SET status = 'completed', completed_at = ?,
                   jobs_found = ?, jobs_new = ?, stats_json = ?
               WHERE id = ?""",
            (datetime.now(UTC).isoformat(), jobs_found, jobs_new, stats_json, scan_id),
        )
        conn.commit()

    def fail_scan_run(self, scan_id: str, error: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE linkedin_scan_runs
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
            """UPDATE linkedin_scan_runs
               SET status = 'paused', completed_at = ?, error = NULL
               WHERE id = ?""",
            (datetime.now(UTC).isoformat(), scan_id),
        )
        conn.commit()

    def interrupt_scan_run(self, scan_id: str) -> None:
        """Record a running scan stopped for backend shutdown: resumable, not failed."""
        mark_run(self._get_conn(), "linkedin_scan_runs", scan_id, "interrupted",
                 SHUTDOWN_RUN_NOTE, ("running",))

    def reopen_scan_run(self, scan_id: str) -> bool:
        """Reopen an interrupted/paused run as the same logical scan (issue #86).

        Keeps started_at, clears completed_at/error. False when no such
        resumable row exists (the caller then records a new run).
        """
        return reopen_run(self._get_conn(), "linkedin_scan_runs", scan_id)

    def abandon_scan_run(self, scan_id: str) -> None:
        """Close an interrupted/paused run whose checkpoint was discarded
        because the config changed, so it does not stay 'interrupted'."""
        mark_run(self._get_conn(), "linkedin_scan_runs", scan_id, "failed",
                 CONFIG_CHANGED_RUN_NOTE, RESUMABLE_RUN_STATUSES)

    def get_scan_run(self, scan_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM linkedin_scan_runs WHERE id = ?", (scan_id,)).fetchone()
        return dict(row) if row else None

    def get_last_scan(self) -> dict[str, Any] | None:
        """Return the most recent scan run record regardless of status."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM linkedin_scan_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def list_scan_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM linkedin_scan_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- resumable scan checkpoint ----------------------------------------------
    #
    # Single-row table: at most one LinkedIn scan can be running at a time
    # (LinkedInScanner._scan_lock), so there is only ever one checkpoint to
    # track. See src/linkedin/scanner.py::_do_scan for how this is used.

    def get_scan_checkpoint(self) -> dict[str, Any] | None:
        """Return the current checkpoint row, or None if there isn't one.

        No checkpoint means either no scan has ever run, or the last scan
        that touched it completed successfully (which clears it).
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM linkedin_scan_checkpoint WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def save_scan_checkpoint(
        self,
        scan_id: str,
        completed_queries: list[str],
        current_query_key: str | None,
        current_page: int,
        state: str = "running",
        fingerprint: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        """Persist which queries are fully done and the in-progress query's page.

        A single UPDATE-or-INSERT of the singleton row, committed
        immediately — the write is small and fast, so it does not hold the
        writer lock long enough to meaningfully contend with concurrent
        activity. Called after every page of the in-progress query, so an
        interruption never loses more than one page's worth of position.

        ``fingerprint`` identifies the scanner config the progress was made
        under; None keeps the stored one (issue #81). ``stats`` are the
        logical scan's running statistics matching this exact position,
        stored in the same write (issue #86); None keeps the stored ones.
        """
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO linkedin_scan_checkpoint
                   (id, scan_id, completed_queries_json, current_query_key, current_page,
                    updated_at, state, config_fingerprint, stats_json)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   scan_id = excluded.scan_id,
                   completed_queries_json = excluded.completed_queries_json,
                   current_query_key = excluded.current_query_key,
                   current_page = excluded.current_page,
                   updated_at = excluded.updated_at,
                   state = excluded.state,
                   config_fingerprint = COALESCE(excluded.config_fingerprint, config_fingerprint),
                   stats_json = COALESCE(excluded.stats_json, stats_json)""",
            (
                scan_id,
                json.dumps(completed_queries),
                current_query_key,
                current_page,
                datetime.now(UTC).isoformat(),
                state,
                fingerprint,
                json.dumps(stats) if stats is not None else None,
            ),
        )
        conn.commit()

    def set_checkpoint_state(
        self, scan_id: str, state: str, create_if_missing: bool = True
    ) -> None:
        """Set the checkpoint's lifecycle state without touching its progress.

        Creates an empty-progress row if none exists yet (e.g. a pause before
        the first page completed: resuming then correctly starts from the
        beginning). Written only by the scanner thread (issue #81).

        With ``create_if_missing=False`` only an existing row is updated (a
        shutdown/failure before any progress leaves no checkpoint behind).
        """
        conn = self._get_conn()
        if not create_if_missing:
            conn.execute(
                "UPDATE linkedin_scan_checkpoint SET state = ?, scan_id = ?, updated_at = ? WHERE id = 1",
                (state, scan_id, datetime.now(UTC).isoformat()),
            )
            conn.commit()
            return
        conn.execute(
            """INSERT INTO linkedin_scan_checkpoint (id, scan_id, updated_at, state)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   state = excluded.state,
                   updated_at = excluded.updated_at""",
            (scan_id, datetime.now(UTC).isoformat(), state),
        )
        conn.commit()

    def clear_scan_checkpoint(self) -> None:
        """Remove the checkpoint — called once a scan completes every query.

        A scan that finishes cleanly has nothing left to resume; the next
        *scheduled* scan should start from page 0 of the first query again
        (that is the normal, intended way to pick up newly-posted jobs),
        not skip everything because of a stale checkpoint.
        """
        conn = self._get_conn()
        conn.execute("DELETE FROM linkedin_scan_checkpoint WHERE id = 1")
        conn.commit()

    # -- status query ---------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return a status snapshot: waiting count and last scan metadata."""
        conn = self._get_conn()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE status = 'waiting'"
        ).fetchone()[0]
        last_row = conn.execute(
            "SELECT * FROM linkedin_scan_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return {
            "waiting": waiting,
            "last_scan": dict(last_row) if last_row else None,
        }

    def count_by_status(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM linkedin_staging GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def record_also_seen_by(
        self, linkedin_job_id: str, query_term: str, scan_run_id: str
    ) -> None:
        """Append query_term to seen_by_queries for a staged job from the current scan.

        Only touches rows that belong to scan_run_id — prior-scan rows and
        filtered-out (never-staged) jobs are unaffected.  A no-op if the job
        is not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT seen_by_queries FROM linkedin_staging "
            "WHERE linkedin_job_id = ? AND scan_run_id = ?",
            (linkedin_job_id, scan_run_id),
        ).fetchone()
        if row is None:
            return
        existing: list[str] = json.loads(row["seen_by_queries"] or "[]")
        if query_term not in existing:
            existing.append(query_term)
        conn.execute(
            "UPDATE linkedin_staging SET seen_by_queries = ? "
            "WHERE linkedin_job_id = ? AND scan_run_id = ?",
            (json.dumps(existing), linkedin_job_id, scan_run_id),
        )
        conn.commit()

    def count_unique_to_query(self, scan_id: str, query_term: str) -> int:
        """Count staged jobs from scan_id discovered exclusively by query_term.

        A job is unique to a query when its seen_by_queries JSON array has
        exactly one element (the discovering query itself).
        """
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) FROM linkedin_staging
               WHERE scan_run_id = ?
                 AND discovered_by_query = ?
                 AND json_array_length(seen_by_queries) = 1""",
            (scan_id, query_term),
        ).fetchone()
        return row[0] if row else 0


# -- conversion ---------------------------------------------------------------

def staging_row_to_job(row: dict[str, Any]) -> Job:
    """Convert a linkedin_staging row to a pipeline-ready Job object.

    Uses the same stable-ID formula as normalize.py so that the job upserted
    into the main jobs table has a consistent, deterministic ID across runs.
    """
    from src.models.job import Job

    linkedin_job_id = row["linkedin_job_id"]

    # Stable ID — identical to what normalize_job() would compute for
    # source="linkedin", source_job_id=linkedin_job_id.
    stable_id = hashlib.sha256(
        f"linkedin:{linkedin_job_id}".encode()
    ).hexdigest()[:32]

    title = row.get("title") or ""
    description = row.get("description") or ""
    company = row.get("company") or ""

    # Content hash (same algorithm as normalize.py: MD5 of title+description).
    # When description is absent (fetch_description=false), include company to
    # prevent same-title/different-company hash collisions that would produce
    # stale embedding cache hits.
    if description:
        content_hash = hashlib.md5((title + " " + description).encode()).hexdigest()
    else:
        content_hash = hashlib.md5((title + " " + company).encode()).hexdigest()

    # Parse published_at
    published_at = None
    raw_pub = row.get("published_at")
    if raw_pub:
        try:
            published_at = datetime.fromisoformat(str(raw_pub))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass

    # Parse discovered_at
    discovered_at = datetime.now(UTC)
    raw_disc = row.get("discovered_at")
    if raw_disc:
        try:
            discovered_at = datetime.fromisoformat(str(raw_disc))
            if discovered_at.tzinfo is None:
                discovered_at = discovered_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass

    from src.pipeline.normalize import normalize_country_code
    _raw_countries: list[str] = json.loads(row.get("countries") or "[]")
    countries: list[str] = [normalize_country_code(c) for c in _raw_countries]
    raw_payload: dict[str, Any] = json.loads(row.get("raw_payload") or "{}")

    # Populate skills from title-extracted skills persisted in raw_payload.
    # When description is unavailable (the common LinkedIn case), these title
    # skills allow the scoring layer to compute a meaningful skills_match score.
    skills: list[str] = raw_payload.get("extracted_title_skills") or []

    return Job(
        id=stable_id,
        source="linkedin",
        source_job_id=linkedin_job_id,
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
        skills=skills,
        raw_payload=raw_payload,
        content_hash=content_hash,
    )
