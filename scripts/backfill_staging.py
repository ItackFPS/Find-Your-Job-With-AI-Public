"""Retroactive backfill for linkedin_staging waiting records.

Applies title-skill extraction, work-mode inference, and employment-type
inference to existing waiting records that predate those features.  Safe to
run multiple times (idempotent: only updates NULL/empty fields).

Usage:
    python scripts/backfill_staging.py [--db data/jobs.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is on sys.path so scanner helpers can be imported.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.linkedin.scanner import (
    _extract_employment_type,
    _extract_title_skills,
    _extract_work_mode,
    _extract_work_mode_from_location,
    _extract_work_mode_from_title,
    _safe_str,
)


def backfill(db_path: str, dry_run: bool = False) -> dict[str, int]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # -- 1. Fix spurious USD records (no salary data) -------------------------
    if dry_run:
        usd_count = conn.execute(
            "SELECT COUNT(*) FROM linkedin_staging "
            "WHERE salary_currency='USD' AND salary_min IS NULL AND salary_max IS NULL"
        ).fetchone()[0]
    else:
        r = conn.execute(
            "UPDATE linkedin_staging SET salary_currency='EUR' "
            "WHERE salary_currency='USD' AND salary_min IS NULL AND salary_max IS NULL"
        )
        conn.commit()
        usd_count = r.rowcount

    # -- 2. Read all waiting records needing enrichment -----------------------
    rows = conn.execute(
        "SELECT id, title, location, is_remote, work_mode, employment_type, raw_payload "
        "FROM linkedin_staging WHERE status='waiting'"
    ).fetchall()

    wm_updated = 0
    et_updated = 0
    skills_updated = 0

    for row in rows:
        row = dict(row)
        title = row.get("title") or ""
        location = row.get("location")
        is_remote = bool(row.get("is_remote", 0))
        raw: dict = json.loads(row.get("raw_payload") or "{}")

        updates: dict[str, object] = {}

        # -- 2a. Work mode --------------------------------------------------
        if not row.get("work_mode"):
            # Try structured fields from raw_payload first
            raw_is_remote = raw.get("is_remote")
            if raw_is_remote is not None:
                try:
                    is_remote_raw = bool(raw_is_remote)
                except (TypeError, ValueError):
                    is_remote_raw = is_remote
            else:
                is_remote_raw = is_remote
            job_type = raw.get("job_type")
            wm = _extract_work_mode(is_remote_raw, job_type)
            if not wm:
                wm = _extract_work_mode_from_title(title) or _extract_work_mode_from_location(location)
            if wm:
                updates["work_mode"] = wm
                if wm == "remote":
                    updates["is_remote"] = 1
                wm_updated += 1

        # -- 2b. Employment type --------------------------------------------
        if not row.get("employment_type"):
            job_type = raw.get("job_type")
            et = _extract_employment_type(job_type, title=title)
            if et:
                updates["employment_type"] = et
                et_updated += 1

        # -- 2c. Title skills -----------------------------------------------
        if "extracted_title_skills" not in raw:
            skills = _extract_title_skills(title)
            if skills:
                raw["extracted_title_skills"] = skills
                updates["raw_payload"] = json.dumps(raw)
                skills_updated += 1

        if updates and not dry_run:
            set_parts = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [row["id"]]
            conn.execute(
                f"UPDATE linkedin_staging SET {set_parts} WHERE id = ?",
                values,
            )

    if not dry_run:
        conn.commit()

    conn.close()

    return {
        "usd_fixed": usd_count,
        "work_mode_backfilled": wm_updated,
        "employment_type_backfilled": et_updated,
        "skills_backfilled": skills_updated,
        "total_waiting": len(rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill linkedin_staging waiting records")
    parser.add_argument("--db", default="data/jobs.db", help="SQLite DB path")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    result = backfill(args.db, dry_run=args.dry_run)
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Backfill results:")
    for k, v in result.items():
        print(f"  {k}: {v}")
