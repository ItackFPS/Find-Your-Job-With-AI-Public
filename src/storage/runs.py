"""Storage for pipeline run records, per-job scores, and rejection records.

Four additive tables (never modifying existing ones):
  pipeline_runs      — one row per pipeline execution
  job_scores         — per-job scores for each run (deterministic + semantic + final)
  job_rejections     — rejected jobs with the filter stage and reason code
  source_checkpoints — one row per collector source, updated only after that
                       source's collection completes with zero errors (Phase
                       NEXT / I11: incremental-collection INFRASTRUCTURE —
                       see record_source_checkpoint_success's docstring. This
                       does not yet change what any collector fetches from
                       its upstream API; it only records, per source, when it
                       last succeeded and how many of its jobs were new vs.
                       already known, as groundwork for a future source-
                       specific incremental fetch).

Phase LATER / I13: job_scores and job_rejections get one brand-new row per
scored/rejected job on EVERY run (never an upsert — see store_job_scores /
store_rejections), so both grow without bound. apply_retention() below
prunes pipeline_runs (and their job_scores/job_rejections) once they are
both older than a configured age AND outside the most-recently-kept window
— it never touches the `jobs` table, so a job that is still active/current
is never affected by retention, regardless of how old the run that
collected it was.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A running row is only recovered when its heartbeat is older than this AND
# its owning process is gone (see recover_stale_runs). Comfortably longer
# than the longest single-stage gap observed in production benchmarks
# (requirements_gate ~228s, Phase 2 as one lump ~482s before per-item
# progress existed) so a genuinely slow-but-alive run is never falsely
# recovered.
_STALE_HEARTBEAT_SECONDS = 600


def _pid_alive(pid: int | None) -> bool:
    """Best-effort, read-only check for whether a process with this PID exists.

    Deliberately does not use ``os.kill(pid, 0)`` as a portable existence
    probe: on Windows, ``os.kill`` with a signal other than CTRL_C/CTRL_BREAK
    calls ``TerminateProcess`` — sending it as a liveness check could kill an
    unrelated process that has since reused the PID. Uses ``OpenProcess``
    (query-only access) via ctypes on Windows, and the real POSIX signal-0
    no-op probe elsewhere. Stdlib only — no new dependency for this.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user
    else:
        return True


class RunStorage:
    """SQLite storage for pipeline run metadata, scores, and rejections."""

    # See Storage._initialized_paths in src/storage/sqlite.py: schema setup
    # only needs to run once per db file per process, not on every request.
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db"):
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
        resolved = str(self._db_path.resolve())
        if resolved in RunStorage._initialized_paths:
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                stats_json TEXT NOT NULL DEFAULT '{}',
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_started ON pipeline_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_status ON pipeline_runs(status);

            CREATE TABLE IF NOT EXISTS job_scores (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                deterministic_score REAL,
                semantic_score REAL,
                final_score REAL,
                score_breakdown_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_job_scores_job_id ON job_scores(job_id);
            CREATE INDEX IF NOT EXISTS idx_job_scores_run_id ON job_scores(run_id);

            CREATE TABLE IF NOT EXISTS job_rejections (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                job_id TEXT,
                rejection_stage TEXT NOT NULL,
                title TEXT,
                company TEXT,
                location TEXT,
                source TEXT,
                rejection_detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_rejections_run_id ON job_rejections(run_id);
            -- get_rejection_for_job() looks up one (run_id, job_id) pair; the
            -- run_id-only index above still requires a linear scan across that
            -- run's rejection rows (thousands, per the funnel stats) to find
            -- the matching job_id. This composite index makes that an exact
            -- lookup. IF NOT EXISTS is idempotent on existing databases —
            -- SQLite backfills the index from current rows on first creation.
            CREATE INDEX IF NOT EXISTS idx_rejections_run_job ON job_rejections(run_id, job_id);

            CREATE TABLE IF NOT EXISTS source_checkpoints (
                source TEXT PRIMARY KEY,
                last_success_at TEXT NOT NULL,
                last_run_id TEXT NOT NULL,
                last_collected_count INTEGER NOT NULL DEFAULT 0,
                last_new_count INTEGER,
                last_known_count INTEGER,
                updated_at TEXT NOT NULL
            );

            -- Per-source circuit breaker (plan F6.1; src/pipeline/circuit_breaker.py).
            -- One row per source name; consecutive_failures counts failed runs in a
            -- row, trips how many pauses this failure streak has caused (drives the
            -- exponential backoff), paused_until is when the source runs again.
            CREATE TABLE IF NOT EXISTS source_breakers (
                source TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                trips INTEGER NOT NULL DEFAULT 0,
                paused_until TEXT,
                last_failure_reason TEXT,
                last_failure_at TEXT,
                updated_at TEXT NOT NULL
            );
        """)

        # Migration: add pid/heartbeat_at columns for restart-safe stale-run
        # recovery (see _pid_alive / recover_stale_runs). ALTER TABLE ADD
        # COLUMN is safe here — nullable, no table rebuild needed. Pre-
        # migration rows get NULL, which recover_stale_runs treats as
        # unconditionally stale (same as the old blanket behavior) since
        # there is no PID/heartbeat evidence to say otherwise.
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)")}
        if "pid" not in existing_columns:
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN pid INTEGER")
        if "heartbeat_at" not in existing_columns:
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN heartbeat_at TEXT")

        conn.commit()
        RunStorage._initialized_paths.add(resolved)

    # -- pipeline_runs CRUD ---------------------------------------------------

    def create_run(self, run_id: str, pid: int | None = None) -> None:
        """Create a new 'running' row, recording the owning process's PID.

        ``pid`` defaults to the current process — the normal case, where the
        pipeline runs as a background thread inside this API process. Callers
        that drive the pipeline from a separate OS process (e.g. an
        externally-run benchmark script) can pass that process's own PID so
        ``recover_stale_runs`` can tell it apart from a genuinely dead run
        after this API process restarts.
        """
        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO pipeline_runs (id, started_at, status, stats_json, pid, heartbeat_at) "
            "VALUES (?, ?, 'running', '{}', ?, ?)",
            (run_id, now, pid if pid is not None else os.getpid(), now),
        )
        conn.commit()

    def update_run_stats(self, run_id: str, stats: dict[str, Any]) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE pipeline_runs SET stats_json = ?, heartbeat_at = ? WHERE id = ?",
            (json.dumps(stats), datetime.now(UTC).isoformat(), run_id),
        )
        conn.commit()

    def complete_run(self, run_id: str, stats: dict[str, Any]) -> None:
        # error is explicitly cleared: a run that was previously marked
        # 'failed' by recover_stale_runs (e.g. a dev-server reload
        # interrupted an earlier attempt tracked under a different row) and
        # later genuinely completes must not keep exposing that stale error
        # string once status is back to 'completed'. Non-fatal warnings
        # collected during a successful run live in stats['errors'] (see
        # PipelineStats.errors) — a separate field, untouched here.
        conn = self._get_conn()
        conn.execute(
            "UPDATE pipeline_runs SET status = 'completed', completed_at = ?, stats_json = ?, error = NULL "
            "WHERE id = ?",
            (datetime.now(UTC).isoformat(), json.dumps(stats), run_id),
        )
        conn.commit()

    def fail_run(self, run_id: str, error: str, stats: dict[str, Any] | None = None) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE pipeline_runs SET status = 'failed', completed_at = ?, error = ?, stats_json = ? WHERE id = ?",
            (
                datetime.now(UTC).isoformat(),
                error,
                json.dumps(stats or {}),
                run_id,
            ),
        )
        conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return _parse_run_row(dict(row))

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_parse_run_row(dict(r)) for r in rows]

    def recover_stale_runs(self) -> int:
        """Mark 'running' rows 'failed' — but only the ones that are actually dead.

        Called on API startup (before any background threads start). The
        naive version of this method used to mark EVERY 'running' row as
        failed unconditionally, on the assumption that a running row can
        only belong to the previous process instance. That assumption breaks
        for a pipeline driven by a separate OS process (e.g. an externally
        run benchmark script): a uvicorn --reload restart would falsely
        declare that still-alive run 'failed' in the DB while the real
        pipeline kept running to completion unaffected — a genuine
        production robustness gap, not just a benchmark artifact, since
        --reload restarts can happen for reasons unrelated to the run itself
        (an unrelated file save in an editor-attached dev environment).

        A row is now only recovered when BOTH:
          1. its recorded ``pid`` is no longer a live process, AND
          2. its ``heartbeat_at`` is older than ``_STALE_HEARTBEAT_SECONDS``
        Either condition alone is not enough — PID reuse after a reboot could
        make a dead run's PID look alive again, and a live-but-just-slow
        run's heartbeat can legitimately lag during a long stage. Requiring
        both keeps genuine crash recovery intact (dead PID + old heartbeat)
        while no longer mis-declaring a still-alive, still-heartbeating
        external process as failed merely because THIS process restarted.

        Rows written before this migration have no pid/heartbeat_at (NULL)
        and are always recovered — the same conservative behavior as before,
        applied only to genuinely ambiguous legacy rows.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, pid, heartbeat_at FROM pipeline_runs WHERE status = 'running'"
        ).fetchall()

        now = datetime.now(UTC)
        recovered = 0
        for row in rows:
            pid = row["pid"]
            heartbeat_at = row["heartbeat_at"]

            stale_heartbeat = True
            if heartbeat_at:
                try:
                    hb = datetime.fromisoformat(heartbeat_at)
                    stale_heartbeat = (now - hb).total_seconds() > _STALE_HEARTBEAT_SECONDS
                except ValueError:
                    stale_heartbeat = True

            if _pid_alive(pid) and not stale_heartbeat:
                continue  # genuinely still running elsewhere — leave it alone

            conn.execute(
                """UPDATE pipeline_runs
                   SET status = 'failed',
                       completed_at = ?,
                       error = 'Run interrupted: owning process is no longer running and no recent heartbeat was recorded.'
                   WHERE id = ?""",
                (now.isoformat(), row["id"]),
            )
            recovered += 1

        conn.commit()
        if recovered:
            logger.warning("Recovered %d stale pipeline run(s) from a previous process.", recovered)
        return recovered

    def get_latest_completed_run(self) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE status = 'completed' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _parse_run_row(dict(row))

    def get_current_running_run(self) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _parse_run_row(dict(row))

    def get_previous_completed_run(self, current_run_id: str) -> dict[str, Any] | None:
        """Return the most recent completed run that started before current_run_id."""
        conn = self._get_conn()
        current = conn.execute(
            "SELECT started_at FROM pipeline_runs WHERE id = ?", (current_run_id,)
        ).fetchone()
        if current is None:
            return None
        row = conn.execute(
            """SELECT * FROM pipeline_runs
               WHERE status = 'completed' AND started_at < ? AND id != ?
               ORDER BY started_at DESC LIMIT 1""",
            (current["started_at"], current_run_id),
        ).fetchone()
        return _parse_run_row(dict(row)) if row else None

    # -- job_scores CRUD ------------------------------------------------------

    def store_job_scores(self, run_id: str, scores: list[dict[str, Any]]) -> None:
        """Persist a batch of job score records for a run."""
        conn = self._get_conn()
        conn.executemany(
            """INSERT OR REPLACE INTO job_scores
               (id, run_id, job_id, deterministic_score, semantic_score, final_score, score_breakdown_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(uuid.uuid4()),
                    run_id,
                    s["job_id"],
                    s.get("deterministic_score"),
                    s.get("semantic_score"),
                    s.get("final_score"),
                    json.dumps(s.get("score_breakdown", {})),
                )
                for s in scores
            ],
        )
        conn.commit()

    def get_job_latest_score(self, job_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT js.*, pr.started_at as run_started_at
               FROM job_scores js
               JOIN pipeline_runs pr ON js.run_id = pr.id
               WHERE js.job_id = ?
               ORDER BY pr.started_at DESC
               LIMIT 1""",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["score_breakdown"] = json.loads(d.pop("score_breakdown_json", "{}"))
        return d

    def get_scores_for_run(self, run_id: str, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return score rows from a specific completed run for the given job IDs.

        Unlike get_latest_scores_for_jobs(), this is scoped to one run — preventing
        partial scores from a concurrent in-progress run from leaking in.
        Returns a dict mapping job_id → score row.
        """
        if not job_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"""SELECT * FROM job_scores
                WHERE run_id = ? AND job_id IN ({placeholders})""",
            [run_id, *job_ids],
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = dict(row)
            d["score_breakdown"] = json.loads(d.pop("score_breakdown_json", "{}"))
            result[d["job_id"]] = d
        return result

    def get_latest_scores_for_jobs(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return the most-recent score row for each job_id in one query.

        Returns a dict mapping job_id → score row (same shape as
        ``get_job_latest_score``).  Missing job_ids are absent from the result.
        Avoids N individual queries when loading a list of jobs.
        """
        if not job_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"""SELECT js.*, pr.started_at as run_started_at
                FROM job_scores js
                JOIN pipeline_runs pr ON js.run_id = pr.id
                WHERE js.job_id IN ({placeholders})
                ORDER BY pr.started_at DESC""",
            job_ids,
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = dict(row)
            jid = d["job_id"]
            if jid not in result:  # keep first row per job_id (highest started_at)
                d["score_breakdown"] = json.loads(d.pop("score_breakdown_json", "{}"))
                result[jid] = d
        return result

    def get_job_ids_for_run(self, run_id: str) -> list[str]:
        """Return job IDs scored in this run, ordered by final_score DESC."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT job_id FROM job_scores WHERE run_id = ?
               ORDER BY final_score DESC NULLS LAST""",
            (run_id,),
        ).fetchall()
        return [r["job_id"] for r in rows]

    def get_jobs_for_run(
        self,
        run_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return job score rows for a specific run, newest first by score."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM job_scores WHERE run_id = ?
               ORDER BY final_score DESC NULLS LAST
               LIMIT ? OFFSET ?""",
            (run_id, limit, offset),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["score_breakdown"] = json.loads(d.pop("score_breakdown_json", "{}"))
            result.append(d)
        return result

    # -- job_rejections CRUD --------------------------------------------------

    def store_rejections(self, run_id: str, rejections: list[dict[str, Any]]) -> None:
        conn = self._get_conn()
        conn.executemany(
            """INSERT INTO job_rejections
               (id, run_id, job_id, rejection_stage, title, company, location, source, rejection_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(uuid.uuid4()),
                    run_id,
                    r.get("job_id"),
                    r.get("rejection_stage", "filter"),
                    r.get("title"),
                    r.get("company"),
                    r.get("location"),
                    r.get("source"),
                    r.get("rejection_detail"),
                )
                for r in rejections
            ],
        )
        conn.commit()

    def get_run_rejections(
        self,
        run_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM job_rejections WHERE run_id = ?
               ORDER BY id LIMIT ? OFFSET ?""",
            (run_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_rejection_for_job(self, run_id: str, job_id: str) -> dict[str, Any] | None:
        """Return the rejection record for a specific job in a specific run, if any."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM job_rejections WHERE run_id = ? AND job_id = ? LIMIT 1",
            (run_id, job_id),
        ).fetchone()
        return dict(row) if row else None

    def count_run_rejections(self, run_id: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM job_rejections WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    # -- source_checkpoints CRUD ------------------------------------------------
    # Phase NEXT / I11: incremental-collection INFRASTRUCTURE only. A
    # checkpoint records that a source's collection completed with zero
    # errors — it is never read by any collector to skip or filter an
    # upstream request today. See record_source_checkpoint_success's
    # docstring for the full correctness contract.

    def record_source_checkpoint_success(
        self,
        source: str,
        run_id: str,
        collected_count: int,
        new_count: int | None,
        known_count: int | None,
        timestamp: str,
    ) -> None:
        """Record that ``source`` completed a collection with zero errors.

        Callers MUST only call this for a source whose collection in this
        run reported no errors — never for a failed or partially-failed
        source, and never speculatively before collection finishes. This is
        the only write path for this table, so that invariant alone is
        enough to guarantee a checkpoint can never advance past a run that
        didn't actually succeed for that source.

        ``new_count``/``known_count`` are optional (``None``) for a source
        where that distinction wasn't computed (e.g. no jobs collected, or
        the caller couldn't classify them) — stored as SQL NULL, distinct
        from an explicit 0, so a reader can tell "not measured" apart from
        "measured as zero".
        """
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO source_checkpoints
                   (source, last_success_at, last_run_id, last_collected_count,
                    last_new_count, last_known_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                   last_success_at=excluded.last_success_at,
                   last_run_id=excluded.last_run_id,
                   last_collected_count=excluded.last_collected_count,
                   last_new_count=excluded.last_new_count,
                   last_known_count=excluded.last_known_count,
                   updated_at=excluded.updated_at""",
            (source, timestamp, run_id, collected_count, new_count, known_count, timestamp),
        )
        conn.commit()

    def get_source_checkpoint(self, source: str) -> dict[str, Any] | None:
        """Return the last successful-collection checkpoint for ``source``,
        or ``None`` if it has never completed a collection without errors."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM source_checkpoints WHERE source = ?", (source,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_source_checkpoints(self) -> list[dict[str, Any]]:
        """Return every source's checkpoint, most recently successful first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM source_checkpoints ORDER BY last_success_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- source_breakers CRUD (plan F6.1) ----------------------------------------

    def list_source_breakers(self) -> list[dict[str, Any]]:
        """Every source's circuit-breaker row (see src/pipeline/circuit_breaker.py)."""
        conn = self._get_conn()
        return [dict(r) for r in conn.execute("SELECT * FROM source_breakers ORDER BY source")]

    def save_source_breakers(self, rows: list[dict[str, Any]]) -> None:
        """Insert or replace breaker rows (keys: the table's columns but updated_at)."""
        if not rows:
            return
        now = datetime.now(UTC).isoformat()
        conn = self._get_conn()
        conn.executemany(
            """INSERT INTO source_breakers
                   (source, consecutive_failures, trips, paused_until,
                    last_failure_reason, last_failure_at, updated_at)
               VALUES (:source, :consecutive_failures, :trips, :paused_until,
                       :last_failure_reason, :last_failure_at, :updated_at)
               ON CONFLICT(source) DO UPDATE SET
                   consecutive_failures=excluded.consecutive_failures,
                   trips=excluded.trips,
                   paused_until=excluded.paused_until,
                   last_failure_reason=excluded.last_failure_reason,
                   last_failure_at=excluded.last_failure_at,
                   updated_at=excluded.updated_at""",
            [{**row, "updated_at": now} for row in rows],
        )
        conn.commit()

    # -- retention (Phase LATER / I13) ------------------------------------------

    def apply_retention(
        self, retention_days: int = 30, keep_min_runs: int = 100
    ) -> dict[str, int]:
        """Delete old pipeline-history rows; NEVER touches the `jobs` table.

        A run is eligible for deletion only when BOTH are true:
          - it is not one of the ``keep_min_runs`` most recently started runs
            (by ``started_at``), regardless of age — this is the floor that
            guarantees the run-history UI (``list_runs``, the per-run detail
            endpoints) never loses data it might actually be showing; and
          - its ``started_at`` is older than ``retention_days`` days ago.

        A ``'running'`` row is never eligible (only ``'completed'``/``'failed'``
        are considered) — an in-progress run must never be pruned out from
        under itself; that is `recover_stale_runs`'s job, not this one's.

        Deletes each eligible run's `job_scores` and `job_rejections` rows
        together with its `pipeline_runs` row, one run at a time, each in its
        own transaction — safe to interrupt (a crash mid-sweep leaves already-
        processed runs cleaned and the rest untouched, exactly as if this call
        had simply been made with fewer eligible runs) and safe to call
        repeatedly (a run already deleted is no longer eligible, so a second
        call deletes nothing more and returns all-zero counts). There are no
        SQL foreign keys in this schema (run_id/job_id are informal string
        links only — see this module's docstring) so cascade behavior is not
        relied upon; every dependent row is deleted explicitly here.

        Returns:
            ``{"runs_deleted": N, "job_scores_deleted": N, "job_rejections_deleted": N}``
        """
        conn = self._get_conn()
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()

        eligible_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT id FROM pipeline_runs
                WHERE status IN ('completed', 'failed')
                  AND started_at < ?
                  AND id NOT IN (
                      SELECT id FROM pipeline_runs
                      WHERE status IN ('completed', 'failed')
                      ORDER BY started_at DESC
                      LIMIT ?
                  )
                """,
                (cutoff, keep_min_runs),
            ).fetchall()
        ]

        runs_deleted = 0
        scores_deleted = 0
        rejections_deleted = 0

        for run_id in eligible_ids:
            try:
                scores_deleted += conn.execute(
                    "DELETE FROM job_scores WHERE run_id = ?", (run_id,)
                ).rowcount
                rejections_deleted += conn.execute(
                    "DELETE FROM job_rejections WHERE run_id = ?", (run_id,)
                ).rowcount
                runs_deleted += conn.execute(
                    "DELETE FROM pipeline_runs WHERE id = ?", (run_id,)
                ).rowcount
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                logger.warning(
                    "Retention: failed to delete run %s — leaving it in place, "
                    "will retry on the next call.",
                    run_id,
                )

        return {
            "runs_deleted": runs_deleted,
            "job_scores_deleted": scores_deleted,
            "job_rejections_deleted": rejections_deleted,
        }


def load_retention_config(config_path: str = "config/retention.yaml") -> dict[str, int]:
    """Load Phase LATER / I13 retention settings, defaulting when absent.

    Deliberately just two scalars (see config/retention.yaml's own comment
    for why 30 days / 100 runs were chosen) rather than a larger settings
    surface — retention has exactly one tunable question ("how much
    pipeline history to keep"), not a family of them.
    """
    defaults = {"retention_days": 30, "keep_min_runs": 100}
    path = Path(config_path)
    if not path.exists():
        return defaults

    import yaml

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise TypeError(f"Expected a mapping in {config_path}")

    return {
        "retention_days": int(data.get("retention_days", defaults["retention_days"])),
        "keep_min_runs": int(data.get("keep_min_runs", defaults["keep_min_runs"])),
    }


# -- helpers ------------------------------------------------------------------

def _parse_run_row(row: dict[str, Any]) -> dict[str, Any]:
    """Parse stats_json from a pipeline_runs row."""
    row["stats"] = json.loads(row.pop("stats_json", "{}"))
    return row
