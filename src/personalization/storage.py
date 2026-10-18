"""SQLite storage for Phase 4: user feedback and a personalization score cache.

Feedback is stored in a new, additive ``job_feedback`` table. The original
``jobs`` table (and every Phase 1-3 table) is never modified. A second table,
``personalization_score_cache``, memoizes the per-job adjustment so that
unchanged jobs are not rescored on every run (the cache is keyed by the
feedback fingerprint + the job's own content hash, so it is invalidated
automatically whenever either changes).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models.job import Job
from src.personalization.models import Feedback, FeedbackType


class PersonalizationStorage:
    """Thin wrapper around SQLite for Phase 4 feedback and score caching."""

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

    # -- schema ----------------------------------------------------------------

    def init_db(self) -> None:
        """Create the feedback and score-cache tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS job_feedback (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                note TEXT,
                company TEXT,
                title TEXT,
                skills TEXT NOT NULL DEFAULT '[]',
                remote INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_job_feedback_job_id ON job_feedback(job_id);
            CREATE INDEX IF NOT EXISTS idx_job_feedback_type ON job_feedback(feedback_type);

            CREATE TABLE IF NOT EXISTS personalization_score_cache (
                job_id TEXT NOT NULL,
                profile_version TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                adjustment REAL NOT NULL,
                explanation JSON NOT NULL,
                computed_at TEXT NOT NULL,
                PRIMARY KEY (job_id, profile_version)
            );
            """
        )
        conn.commit()

    # -- feedback CRUD -----------------------------------------------------

    def add_feedback(
        self,
        job: Job,
        feedback_type: FeedbackType,
        note: str | None = None,
    ) -> Feedback:
        """Record feedback for a job, snapshotting its key attributes.

        Snapshotting (company/title/skills/remote) means the preference
        profile can still be built from this feedback later even if the
        job itself is updated or pruned from the ``jobs`` table.
        """
        feedback = Feedback(
            job_id=job.id,
            feedback_type=feedback_type,
            note=note,
            company=job.company,
            title=job.title,
            skills=list(job.skills),
            remote=job.remote,
        )
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO job_feedback
               (id, job_id, feedback_type, note, company, title, skills, remote, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feedback.id,
                feedback.job_id,
                feedback.feedback_type.value,
                feedback.note,
                feedback.company,
                feedback.title,
                json.dumps(feedback.skills),
                int(feedback.remote),
                feedback.created_at.isoformat(),
            ),
        )
        conn.commit()
        return feedback

    def get_feedback_for_job(self, job_id: str) -> list[Feedback]:
        """Return all feedback recorded for a specific job, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM job_feedback WHERE job_id = ? ORDER BY created_at DESC",
            (job_id,),
        ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def get_all_feedback(self) -> list[Feedback]:
        """Return every stored feedback record, oldest first."""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM job_feedback ORDER BY created_at ASC").fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def count_feedback(self) -> int:
        """Return the total number of stored feedback records."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()
        return int(row[0]) if row else 0

    def get_feedback_version(self) -> str:
        """Return a short, stable fingerprint of all currently stored feedback.

        The fingerprint changes whenever feedback is added, so it can be used
        as a cheap cache key: unchanged feedback -> unchanged fingerprint ->
        no need to rebuild the preference profile or rescore unchanged jobs.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), '') AS latest FROM job_feedback"
        ).fetchone()
        fingerprint = f"{row['n']}:{row['latest']}"
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _row_to_feedback(row: sqlite3.Row) -> Feedback:
        data = dict(row)
        data["feedback_type"] = FeedbackType(data["feedback_type"])
        data["skills"] = json.loads(data["skills"]) if data.get("skills") else []
        data["remote"] = bool(data["remote"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        return Feedback(**data)

    # -- personalization score cache ----------------------------------------

    def get_cached_score(
        self,
        job_id: str,
        profile_version: str,
        content_hash: str,
    ) -> tuple[float, dict[str, Any]] | None:
        """Return a cached (adjustment, explanation) pair, if still valid.

        The cache entry is considered valid only when both the feedback
        fingerprint (``profile_version``) and the job's own ``content_hash``
        still match what was cached, so any change to either the feedback
        history or the job content invalidates it automatically.
        """
        conn = self._get_conn()
        row = conn.execute(
            """SELECT adjustment, explanation, content_hash FROM personalization_score_cache
               WHERE job_id = ? AND profile_version = ?""",
            (job_id, profile_version),
        ).fetchone()
        if row is None or row["content_hash"] != content_hash:
            return None
        explanation: dict[str, Any] = json.loads(row["explanation"])
        return float(row["adjustment"]), explanation

    def cache_score(
        self,
        job_id: str,
        profile_version: str,
        content_hash: str,
        adjustment: float,
        explanation: dict[str, Any],
    ) -> None:
        """Store a computed adjustment/explanation for later reuse."""
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO personalization_score_cache
               (job_id, profile_version, content_hash, adjustment, explanation, computed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                profile_version,
                content_hash,
                adjustment,
                json.dumps(explanation),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()

    def get_cached_scores_bulk(
        self,
        job_ids: list[str],
        profile_version: str,
        content_hashes: dict[str, str],
    ) -> dict[str, tuple[float, dict[str, Any]]]:
        """Bulk variant of get_cached_score — one SELECT for all jobs.

        Returns only entries where the stored content_hash matches the current
        content_hash for that job, so stale entries are silently excluded.

        Args:
            job_ids:        IDs to look up.
            profile_version: Current feedback fingerprint.
            content_hashes: Mapping of job_id → current content_hash.

        Returns:
            Mapping of job_id → (adjustment, explanation) for cache hits only.
        """
        if not job_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"""SELECT job_id, content_hash, adjustment, explanation
                FROM personalization_score_cache
                WHERE job_id IN ({placeholders}) AND profile_version = ?""",
            (*job_ids, profile_version),
        ).fetchall()

        result: dict[str, tuple[float, dict[str, Any]]] = {}
        for row in rows:
            jid = row["job_id"]
            if content_hashes.get(jid) == row["content_hash"]:
                result[jid] = (float(row["adjustment"]), json.loads(row["explanation"]))
        return result

    def cache_scores_bulk(
        self,
        records: list[tuple[str, str, str, float, dict[str, Any]]],
        profile_version: str,
    ) -> None:
        """Bulk variant of cache_score — single transaction for all records.

        Args:
            records:         List of (job_id, content_hash, computed_at, adjustment, explanation).
            profile_version: Current feedback fingerprint to store with each row.
        """
        if not records:
            return
        conn = self._get_conn()
        conn.executemany(
            """INSERT OR REPLACE INTO personalization_score_cache
               (job_id, profile_version, content_hash, adjustment, explanation, computed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (job_id, profile_version, content_hash, adjustment,
                 json.dumps(explanation), computed_at)
                for job_id, content_hash, computed_at, adjustment, explanation in records
            ],
        )
        conn.commit()
