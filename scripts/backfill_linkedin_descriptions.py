"""Backfill missing descriptions for existing LinkedIn staging records.

Fetches the full description from LinkedIn's guest API for every
linkedin_staging record that has no description (description IS NULL or '').

For jobs already transferred to the main jobs table, also updates that table
and the FTS index so the recovered text is immediately searchable.

Safe to run multiple times — only touches records where description_fetched=0.

Usage:
    python scripts/backfill_linkedin_descriptions.py [--db data/jobs.db]
        [--dry-run] [--delay SECS] [--limit N] [--status STATUS]

Arguments:
    --db      Path to the SQLite database (default: data/jobs.db)
    --dry-run Report what would be done without writing anything
    --delay   Seconds between description fetch requests (default: 5.0)
    --limit   Maximum number of records to process in one run (default: all)
    --status  Only process records in this status: waiting, transferred,
              processed, all (default: all)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _compute_content_hash(title: str, description: str, company: str) -> str:
    if description:
        return hashlib.md5((title + " " + description).encode()).hexdigest()
    return hashlib.md5((title + " " + company).encode()).hexdigest()


def backfill(
    db_path: str,
    dry_run: bool = False,
    delay: float = 5.0,
    limit: int | None = None,
    status_filter: str = "all",
) -> dict[str, int]:
    """Fetch and store descriptions for linkedin_staging rows that lack them.

    description_fetched values:
      0 — not yet fetched (or failed with a retryable error: login_gated/rate_limited)
      1 — successfully fetched
      2 — permanently unavailable (job deleted / HTTP 404 or 410); never retried

    Returns a dict with counts: attempted, fetched, skipped_login_gate, deleted,
    rate_limited, failed, staging_updated, jobs_table_updated.
    """
    from src.linkedin.scanner import (
        fetch_linkedin_description_classified,
        _extract_work_mode_from_text,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # description_fetched=0: not fetched / retryable failure
    # description_fetched=2: permanently gone (404) — excluded from retries
    where_clauses = [
        "description_fetched = 0",
        "(description IS NULL OR TRIM(description) = '')",
    ]
    if status_filter != "all":
        where_clauses.append(f"status = '{status_filter}'")

    limit_clause = f" LIMIT {limit}" if limit else ""
    query = (
        "SELECT id, linkedin_job_id, title, company, location, work_mode, "
        "raw_payload, status FROM linkedin_staging WHERE "
        + " AND ".join(where_clauses)
        + " ORDER BY discovered_at DESC"
        + limit_clause
    )
    rows = conn.execute(query).fetchall()
    rows = [dict(r) for r in rows]

    counts = {
        "total_eligible": len(rows),
        "attempted": 0,
        "fetched": 0,
        "skipped_login_gate": 0,
        "deleted": 0,
        "rate_limited": 0,
        "failed": 0,
        "staging_updated": 0,
        "jobs_table_updated": 0,
    }

    if not rows:
        logger.info("No linkedin_staging records need description backfill.")
        conn.close()
        return counts

    logger.info(
        "Found %d linkedin_staging record(s) with missing descriptions "
        "(status_filter=%s, dry_run=%s).",
        len(rows),
        status_filter,
        dry_run,
    )

    for i, row in enumerate(rows):
        job_id = row["linkedin_job_id"]
        title = row.get("title") or ""
        company = row.get("company") or ""

        if i > 0 and delay > 0:
            time.sleep(delay)

        counts["attempted"] += 1
        logger.info(
            "[%d/%d] Fetching description for job %s (%r)…",
            i + 1, len(rows), job_id, title[:60],
        )

        try:
            description, fetch_status = fetch_linkedin_description_classified(job_id)
        except Exception as exc:
            logger.warning("  fetch raised: %s — skipping.", exc)
            counts["failed"] += 1
            continue

        if fetch_status == "deleted":
            logger.info("  Job %s is gone (404/410) — marking permanently unavailable.", job_id)
            counts["deleted"] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE linkedin_staging SET description_fetched = 2 "
                    "WHERE linkedin_job_id = ? AND description_fetched = 0",
                    (job_id,),
                )
                conn.commit()
            continue

        if fetch_status == "rate_limited":
            logger.warning("  Rate-limited on job %s — stopping this run.", job_id)
            counts["rate_limited"] += 1
            break  # stop processing; retry later

        if not description:
            logger.debug("  %s for job %s — skipping.", fetch_status, job_id)
            counts["skipped_login_gate"] += 1
            continue

        counts["fetched"] += 1
        logger.info("  Got %d chars of description.", len(description))

        if dry_run:
            counts["staging_updated"] += 1
            logger.info("  [DRY RUN] Would update linkedin_staging for job %s.", job_id)
            continue

        # Derive work_mode from description if not already set.
        new_work_mode = row.get("work_mode")
        if not new_work_mode:
            wm = _extract_work_mode_from_text(description)
            if wm:
                new_work_mode = wm
                logger.debug("  Derived work_mode=%r from description.", wm)

        # Update raw_payload to include description.
        raw: dict = json.loads(row.get("raw_payload") or "{}")
        raw["description"] = description

        conn.execute(
            """UPDATE linkedin_staging
               SET description = ?,
                   description_fetched = 1,
                   work_mode = COALESCE(work_mode, ?),
                   is_remote = CASE
                       WHEN work_mode IS NULL AND ? = 'remote' THEN 1
                       ELSE is_remote
                   END,
                   raw_payload = ?
               WHERE linkedin_job_id = ? AND description_fetched = 0""",
            (
                description,
                new_work_mode,
                new_work_mode,
                json.dumps(raw),
                job_id,
            ),
        )
        conn.commit()
        counts["staging_updated"] += 1

        # For transferred jobs, also update the main jobs table + FTS.
        if row.get("status") == "transferred":
            stable_id = row["id"]
            new_hash = _compute_content_hash(title, description, company)

            # Only update if the jobs table row also lacks a description.
            jobs_row = conn.execute(
                "SELECT id, title, company, skills FROM jobs "
                "WHERE id = ? AND source = 'linkedin' "
                "AND (description IS NULL OR TRIM(description) = '')",
                (stable_id,),
            ).fetchone()

            if jobs_row:
                skills_str = jobs_row["skills"] or "[]"
                conn.execute(
                    """UPDATE jobs
                       SET description = ?,
                           content_hash = ?,
                           work_mode = COALESCE(work_mode, ?)
                       WHERE id = ? AND source = 'linkedin'""",
                    (description, new_hash, new_work_mode, stable_id),
                )
                # Rebuild FTS entry for this job.
                conn.execute("DELETE FROM job_fts WHERE id = ?", (stable_id,))
                conn.execute(
                    "INSERT INTO job_fts (id, title, description, company, skills) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        stable_id,
                        jobs_row["title"] or "",
                        description,
                        jobs_row["company"] or "",
                        skills_str,
                    ),
                )
                conn.commit()
                counts["jobs_table_updated"] += 1
                logger.info(
                    "  Updated jobs table + FTS for transferred job %s.", stable_id
                )

    conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing descriptions for LinkedIn staging records"
    )
    parser.add_argument(
        "--db", default="data/jobs.db", help="Path to SQLite database"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without writing",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=5.0,
        help="Seconds between description fetch requests (default: 5.0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum records to process (default: all)",
    )
    parser.add_argument(
        "--status",
        default="all",
        choices=["waiting", "transferred", "processed", "all"],
        help="Only process records in this status (default: all)",
    )
    args = parser.parse_args()

    result = backfill(
        db_path=args.db,
        dry_run=args.dry_run,
        delay=args.delay,
        limit=args.limit,
        status_filter=args.status,
    )

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{prefix}Backfill results:")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
