"""Retroactive backfill for LinkedIn jobs with empty ``skills`` in the ``jobs`` table.

Fixes the effect of a bug where LinkedIn's skill extraction only scanned the
job title (never the description, even when already fetched) using a
narrower vocabulary than every other source's normalize.py fallback. Once
src/linkedin/scanner.py's extraction is fixed, this backfill recomputes
skills for existing LinkedIn rows so already-collected jobs benefit too,
without a full pipeline re-scan.

Idempotent: only rows where ``skills`` is currently empty (`[]`) are updated.
Never touches non-LinkedIn rows or rows that already have skills.

Usage:
    python scripts/backfill_linkedin_job_skills.py [--db data/jobs.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.linkedin.scanner import _extract_title_skills
from src.requirements.principal_skill import extract_known_technologies


def backfill(db_path: str, dry_run: bool = False) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, description, skills FROM jobs "
        "WHERE source = 'linkedin' AND (skills IS NULL OR skills = '[]')"
    ).fetchall()

    updated = 0
    for row in rows:
        title = row["title"] or ""
        description = row["description"] or ""
        skills = list(dict.fromkeys(
            _extract_title_skills(title) + extract_known_technologies(description)
        ))
        if not skills:
            continue
        updated += 1
        if not dry_run:
            conn.execute(
                "UPDATE jobs SET skills = ? WHERE id = ?",
                (json.dumps(skills), row["id"]),
            )
            conn.execute(
                "UPDATE job_fts SET skills = ? WHERE id = ?",
                (" ".join(skills), row["id"]),
            )

    if not dry_run:
        conn.commit()
    conn.close()

    return {"linkedin_jobs_checked": len(rows), "skills_backfilled": updated}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill empty skills on LinkedIn jobs")
    parser.add_argument("--db", default="data/jobs.db", help="SQLite DB path")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    result = backfill(args.db, dry_run=args.dry_run)
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Backfill results:")
    for k, v in result.items():
        print(f"  {k}: {v}")
