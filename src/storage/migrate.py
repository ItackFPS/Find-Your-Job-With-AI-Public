"""Database migration utilities.

Currently provides one migration:

    deduplicate_jobs_db(db_path)  — collapse duplicate job rows that
        accumulated when job IDs were random UUIDs instead of stable hashes.

After Phase 1 (stable IDs in normalize.py), new pipeline runs will never
create duplicates.  This migration cleans up the existing accumulation so the
FTS table shrinks back to its correct size and personalization/embedding caches
become effective immediately.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _stable_id(source: str, source_job_id: str) -> str:
    """Compute the canonical deterministic job ID.

    Must match the formula used in src/pipeline/normalize.py.
    """
    return hashlib.sha256(
        f"{source}:{source_job_id}".encode()
    ).hexdigest()[:32]


def deduplicate_jobs_db(
    db_path: str = "data/jobs.db",
    *,
    backup: bool = True,
    dry_run: bool = False,
) -> dict:
    """Deduplicate the jobs table by collapsing rows that share the same
    (source, source_job_id) into a single canonical row with the stable ID.

    Steps:
        1. Optional backup (data/jobs.db → data/jobs_backup_<ts>.db)
        2. For each unique (source, source_job_id) group with >1 row:
           a. Compute canonical stable ID
           b. Select the best row (latest last_seen_at / discovered_at)
           c. INSERT OR REPLACE a row with the canonical ID
           d. Delete all other rows in the group (both UUID and any duplicate)
        3. Clean orphaned personalization_score_cache entries
        4. Rebuild the FTS table from the deduplicated jobs table
        5. VACUUM to reclaim disk space
        6. Integrity check

    Args:
        db_path:  Path to the SQLite database.
        backup:   If True, copy the DB before making changes.
        dry_run:  If True, analyse and report without committing any changes.

    Returns:
        A dict with summary statistics.
    """
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    stats: dict = {
        "backup_path": None,
        "dry_run": dry_run,
        "groups_processed": 0,
        "rows_before": 0,
        "rows_after": 0,
        "duplicates_removed": 0,
        "fts_rebuilt": False,
        "pers_cache_cleaned": 0,
        "vacuum": False,
        "integrity_ok": False,
        "started_at": datetime.now(UTC).isoformat(),
    }

    # ── 1. Backup ─────────────────────────────────────────────────────────────
    if backup and not dry_run:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_path = db.parent / f"{db.stem}_backup_{ts}{db.suffix}"
        shutil.copy2(db, backup_path)
        stats["backup_path"] = str(backup_path)
        logger.info("Backup created: %s", backup_path)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # See src/linkedin/staging.py for why this is needed: several modules
    # open their own connection to the same jobs.db file.
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        # ── 2. Count rows before ──────────────────────────────────────────────
        stats["rows_before"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        logger.info("Rows before migration: %d", stats["rows_before"])

        # ── 3. Find all (source, source_job_id) groups ───────────────────────
        # Include every group that has a source_job_id — whether it has
        # duplicates (UUID era) or simply carries the wrong UUID-based ID.
        # The loop below re-assigns canonical IDs and removes extras.
        groups = conn.execute("""
            SELECT source, source_job_id, COUNT(*) AS cnt
            FROM jobs
            WHERE source_job_id IS NOT NULL AND source_job_id != ''
            GROUP BY source, source_job_id
        """).fetchall()

        logger.info(
            "Found %d (source, source_job_id) groups to process", len(groups)
        )
        stats["groups_processed"] = len(groups)

        if dry_run:
            dup_groups = [r for r in groups if r["cnt"] > 1]
            stats["duplicates_removed"] = sum(r["cnt"] - 1 for r in dup_groups)
            logger.info(
                "[DRY RUN] Would remove %d duplicate rows across %d groups",
                stats["duplicates_removed"],
                len(dup_groups),
            )
            return stats

        # ── 4. Process each group ─────────────────────────────────────────────
        total_removed = 0

        for group in groups:
            source = group["source"]
            source_job_id = group["source_job_id"]
            canonical_id = _stable_id(source, source_job_id)

            # Fetch all rows for this group, newest first (for content fields).
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE source = ? AND source_job_id = ?
                   ORDER BY COALESCE(last_seen_at, discovered_at) DESC""",
                (source, source_job_id),
            ).fetchall()

            if not rows:
                continue

            all_ids = [r["id"] for r in rows]

            # Best row = most recent (for content and last_seen_at).
            best = dict(rows[0])

            # Preserve earliest first_seen_at across all rows in the group.
            earliest_first_seen = min(
                (r["first_seen_at"] or r["discovered_at"] for r in rows),
                default=best["discovered_at"],
            )
            best["first_seen_at"] = earliest_first_seen

            # Assign canonical ID.
            best["id"] = canonical_id
            _insert_job_row(conn, best)

            # Delete every non-canonical row in this group.
            non_canonical = [jid for jid in all_ids if jid != canonical_id]
            if non_canonical:
                conn.execute(
                    "DELETE FROM jobs WHERE id IN ({})".format(
                        ",".join("?" * len(non_canonical))
                    ),
                    non_canonical,
                )
                total_removed += len(non_canonical)

        conn.commit()
        stats["duplicates_removed"] = total_removed
        logger.info("Removed %d duplicate rows", total_removed)

        # ── 5. Clean orphaned personalization_score_cache ─────────────────────
        try:
            result = conn.execute(
                """DELETE FROM personalization_score_cache
                   WHERE job_id NOT IN (SELECT id FROM jobs)"""
            )
            stats["pers_cache_cleaned"] = result.rowcount
            conn.commit()
            logger.info(
                "Cleaned %d orphaned personalization cache rows",
                result.rowcount,
            )
        except sqlite3.OperationalError:
            pass  # Table may not exist yet

        # ── 6. Rebuild FTS table ──────────────────────────────────────────────
        conn.execute("DROP TABLE IF EXISTS job_fts")
        conn.execute(
            """CREATE VIRTUAL TABLE job_fts USING fts5(
                id UNINDEXED, title, description, company, skills,
                tokenize="unicode61 remove_diacritics 2"
            )"""
        )
        conn.execute(
            """INSERT INTO job_fts (id, title, description, company, skills)
               SELECT id,
                      COALESCE(title, ''),
                      COALESCE(description, ''),
                      COALESCE(company, ''),
                      COALESCE(skills, '')
               FROM jobs"""
        )
        conn.commit()
        stats["fts_rebuilt"] = True
        logger.info("FTS table rebuilt")

        # ── 7. VACUUM ─────────────────────────────────────────────────────────
        conn.execute("VACUUM")
        stats["vacuum"] = True
        logger.info("VACUUM complete")

        # ── 8. Integrity check ────────────────────────────────────────────────
        row = conn.execute("PRAGMA integrity_check").fetchone()
        stats["integrity_ok"] = (row[0] == "ok")
        if stats["integrity_ok"]:
            logger.info("Integrity check: OK")
        else:
            logger.error("Integrity check FAILED: %s", row[0])

        stats["rows_after"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        logger.info(
            "Migration complete: %d → %d rows (%d removed)",
            stats["rows_before"],
            stats["rows_after"],
            stats["duplicates_removed"],
        )

    finally:
        conn.close()

    return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_job_row(conn: sqlite3.Connection, data: dict) -> None:
    """INSERT OR REPLACE a jobs row from a plain dict (field order matches schema)."""
    conn.execute(
        """INSERT OR REPLACE INTO jobs (
            id, source, source_job_id, canonical_url, apply_url, company, title,
            description, location, countries, remote, employment_type,
            salary_min, salary_max, salary_currency, published_at, updated_at,
            discovered_at, skills, raw_payload, content_hash,
            first_seen_at, last_seen_at, status
        ) VALUES (
            :id, :source, :source_job_id, :canonical_url, :apply_url, :company, :title,
            :description, :location, :countries, :remote, :employment_type,
            :salary_min, :salary_max, :salary_currency, :published_at, :updated_at,
            :discovered_at, :skills, :raw_payload, :content_hash,
            :first_seen_at, :last_seen_at, :status
        )""",
        data,
    )
