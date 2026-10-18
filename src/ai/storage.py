"""SQLite storage for Phase 2: Local AI Job Understanding results.

Adds an additive table to store AI analysis results without overwriting
the original job data. The existing job remains intact; the analysis is
stored as a separate, linked record.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.ai.models import (
    EligibilityDecision,
    EligibilityResult,
    JobUnderstanding,
    JobUnderstandingResult,
)


class AIAnalysisStorage:
    """Thin wrapper around SQLite for storing Phase 2 AI analysis results.

    The original job data is never modified. Analysis results are stored
    in a separate table linked by job_id.
    """

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- connection helpers --------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            # See src/linkedin/staging.py for why this is needed: several
            # modules open their own connection to the same jobs.db file.
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema --------------------------------------------------------------

    def init_db(self) -> None:
        """Create the analysis results table if it doesn't exist.

        ``content_hash`` and ``profile_version`` make cache validity match the
        convention already used by the embedding cache (``storage/sqlite.py``)
        and the personalization score cache (``personalization/storage.py``):
        a cached row is only reused when the job's content AND the profile
        used to produce it are unchanged. Older databases created before these
        columns existed are migrated additively via ALTER TABLE — existing rows
        are kept (never dropped), they simply carry an empty content_hash/
        profile_version until next written, which correctly counts as a cache
        miss rather than serving a stale analysis.
        """
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ai_analysis (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                analysis JSON NOT NULL,
                prompt_version TEXT NOT NULL,
                model_identifier TEXT,
                content_hash TEXT NOT NULL DEFAULT '',
                profile_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'success',
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ai_analysis_job_id ON ai_analysis(job_id);
            CREATE INDEX IF NOT EXISTS idx_ai_analysis_status ON ai_analysis(status);
            CREATE INDEX IF NOT EXISTS idx_ai_analysis_prompt_version ON ai_analysis(prompt_version);
        """)
        for column, coltype in (("content_hash", "TEXT NOT NULL DEFAULT ''"), ("profile_version", "TEXT NOT NULL DEFAULT ''")):
            try:
                conn.execute(f"ALTER TABLE ai_analysis ADD COLUMN {column} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists (pre-existing database)
        conn.commit()

    # -- CRUD ----------------------------------------------------------------

    def upsert_analysis(
        self,
        result: JobUnderstandingResult,
        content_hash: str = "",
        profile_version: str = "",
    ) -> None:
        """Store or update an AI analysis result for a job.

        ``content_hash``/``profile_version`` default to "" for callers that
        don't track them (e.g. existing tests) — a cache lookup only ever
        matches when both the stored and the queried value are equal, so an
        empty default is simply never a cache hit against a real job/profile,
        never a false one.
        """
        conn = self._get_conn()
        analysis_json = json.dumps(result.analysis.model_dump())
        now = datetime.now(UTC).isoformat()
        # One cached analysis per (job, prompt_version): keying solely off
        # analysis content would collide across different jobs, so the row
        # id must be derived from job_id, not from the analysis payload.
        record_id = f"{result.job_id}::{result.prompt_version}"

        conn.execute(
            """INSERT OR REPLACE INTO ai_analysis
               (id, job_id, analysis, prompt_version, model_identifier, content_hash, profile_version, status, error_message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id,
                result.job_id,
                analysis_json,
                result.prompt_version,
                result.model_identifier,
                content_hash,
                profile_version,
                result.status,
                result.error_message,
                now,
                now,
            ),
        )
        conn.commit()

    def get_analysis(
        self,
        job_id: str,
        prompt_version: str | None = None,
        content_hash: str = "",
        profile_version: str = "",
    ) -> JobUnderstandingResult | None:
        """Retrieve the AI analysis for a specific job, if still valid.

        When ``prompt_version`` is given, only a row for that exact prompt
        version is considered (a job can have multiple cached analyses under
        different prompt versions, e.g. ``job_understanding_v1`` and
        ``eligibility_gate_v1``). The row is only returned when its stored
        ``content_hash``/``profile_version`` match the ones passed in —
        callers that don't pass them (default "") only match rows stored the
        same way, so existing callers keep working unchanged.
        """
        conn = self._get_conn()
        if prompt_version is not None:
            row = conn.execute(
                "SELECT * FROM ai_analysis WHERE job_id = ? AND prompt_version = ?",
                (job_id, prompt_version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM ai_analysis WHERE job_id = ?", (job_id,)
            ).fetchone()

        if row is None:
            return None
        if row["content_hash"] != content_hash or row["profile_version"] != profile_version:
            return None

        analysis_json = row["analysis"]
        try:
            analysis = json.loads(analysis_json)
        except (json.JSONDecodeError, ValueError):
            analysis = {}

        return JobUnderstandingResult(
            job_id=row["job_id"],
            analysis=JobUnderstanding(**analysis),
            prompt_version=row["prompt_version"],
            model_identifier=row["model_identifier"],
            status=row["status"],
            error_message=row["error_message"],
        )

    def get_analyses_for_jobs(
        self,
        job_ids: list[str],
        prompt_version: str,
        content_hashes: dict[str, str] | None = None,
        profile_version: str = "",
    ) -> dict[str, JobUnderstandingResult]:
        """Retrieve valid cached analyses for multiple jobs under one prompt version.

        ``prompt_version`` is required (not optional): the table holds rows for
        several distinct prompt versions (Phase 2 job understanding, the
        eligibility gate, ...) under the same job_id, so a lookup that ignored
        it could return the wrong stage's cached result for a job. A row is
        only returned when its stored ``content_hash`` matches
        ``content_hashes[job_id]`` and its stored ``profile_version`` matches
        ``profile_version`` — any mismatch (job content changed, profile
        changed) is silently treated as a cache miss, never a stale hit.
        """
        if not job_ids:
            return {}
        content_hashes = content_hashes or {}

        conn = self._get_conn()
        placeholders = ",".join(["?"] * len(job_ids))
        rows = conn.execute(
            f"SELECT * FROM ai_analysis WHERE job_id IN ({placeholders}) AND prompt_version = ?",
            (*job_ids, prompt_version),
        ).fetchall()

        results: dict[str, JobUnderstandingResult] = {}
        for row in rows:
            jid = row["job_id"]
            if row["content_hash"] != content_hashes.get(jid, "") or row["profile_version"] != profile_version:
                continue

            analysis_json = row["analysis"]
            try:
                analysis = json.loads(analysis_json)
            except (json.JSONDecodeError, ValueError):
                analysis = {}

            results[jid] = JobUnderstandingResult(
                job_id=jid,
                analysis=JobUnderstanding(**analysis),
                prompt_version=row["prompt_version"],
                model_identifier=row["model_identifier"],
                status=row["status"],
                error_message=row["error_message"],
            )

        return results

    # -- eligibility gate CRUD ------------------------------------------------
    # Reuses the same ai_analysis table/columns as the JobUnderstanding CRUD
    # above (id, job_id, analysis JSON, prompt_version, content_hash,
    # profile_version, ...) so the two LLM stages share one cache mechanism
    # and one migration path — only the JSON payload's shape differs
    # (EligibilityDecision vs JobUnderstanding). Rows never collide across
    # stages because prompt_version is always part of the lookup/id.

    def upsert_eligibility(
        self,
        result: EligibilityResult,
        content_hash: str = "",
        profile_version: str = "",
    ) -> None:
        """Store or update an eligibility decision for a job."""
        conn = self._get_conn()
        decision_json = json.dumps(result.decision.model_dump())
        now = datetime.now(UTC).isoformat()
        record_id = f"{result.job_id}::{result.prompt_version}"

        conn.execute(
            """INSERT OR REPLACE INTO ai_analysis
               (id, job_id, analysis, prompt_version, model_identifier, content_hash, profile_version, status, error_message, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id,
                result.job_id,
                decision_json,
                result.prompt_version,
                result.model_identifier,
                content_hash,
                profile_version,
                result.status,
                result.error_message,
                now,
                now,
            ),
        )
        conn.commit()

    def get_eligibility_for_jobs(
        self,
        job_ids: list[str],
        prompt_version: str,
        content_hashes: dict[str, str] | None = None,
        profile_version: str = "",
    ) -> dict[str, EligibilityResult]:
        """Retrieve valid cached eligibility decisions for multiple jobs.

        Same validity rule as ``get_analyses_for_jobs``: a row is only
        returned when its stored content_hash/profile_version match what's
        passed in.
        """
        if not job_ids:
            return {}
        content_hashes = content_hashes or {}

        conn = self._get_conn()
        placeholders = ",".join(["?"] * len(job_ids))
        rows = conn.execute(
            f"SELECT * FROM ai_analysis WHERE job_id IN ({placeholders}) AND prompt_version = ?",
            (*job_ids, prompt_version),
        ).fetchall()

        results: dict[str, EligibilityResult] = {}
        for row in rows:
            jid = row["job_id"]
            if row["content_hash"] != content_hashes.get(jid, "") or row["profile_version"] != profile_version:
                continue

            try:
                decision_data = json.loads(row["analysis"])
                decision = EligibilityDecision(**decision_data)
            except (json.JSONDecodeError, ValueError, TypeError):
                decision = EligibilityDecision()

            results[jid] = EligibilityResult(
                job_id=jid,
                decision=decision,
                prompt_version=row["prompt_version"],
                model_identifier=row["model_identifier"],
                status=row["status"],
                error_message=row["error_message"],
            )

        return results

    def count_analyses(self) -> int:
        """Count the total number of stored analyses."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM ai_analysis").fetchone()
        return int(row[0]) if row else 0

    def get_failed_analyses(self) -> list[JobUnderstandingResult]:
        """Retrieve all failed analyses."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM ai_analysis WHERE status = 'failed' ORDER BY created_at DESC"
        ).fetchall()

        results: list[JobUnderstandingResult] = []
        for row in rows:
            analysis_json = row["analysis"]
            try:
                analysis = json.loads(analysis_json)
            except (json.JSONDecodeError, ValueError):
                analysis = {}

            results.append(JobUnderstandingResult(
                job_id=row["job_id"],
                analysis=JobUnderstanding(**analysis),
                prompt_version=row["prompt_version"],
                model_identifier=row["model_identifier"],
                status=row["status"],
                error_message=row["error_message"],
            ))

        return results
