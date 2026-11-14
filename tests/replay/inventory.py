"""Orphan inventory (Phase B B2-B): references to an offer that has no ``jobs`` row, per table.

Read-only (the database is opened ``mode=ro``). Counts only, never offer content. Used by the replay
report before and after each run, so "zero NEW orphans" can be checked against what already existed.

What counts as an orphan (an offer the application refers to but cannot show):

* ``accepted_decisions``: latest-or-not accepted decisions whose offer has no row (an accepted offer is
  always stored, so each one is a defect: P-031);
* ``scores``: ``job_scores`` rows whose offer has no row (scores exist only for accepted offers);
* ``feedback_events`` / ``exposures``: user events on an offer with no row. These are reported but are
  not all defects: the Review page can show a rejected offer's stored text, and feedback on it is
  legitimate.

Rejected decisions without a row are normal (rejected offers are not stored) and are not counted.

    python -m tests.replay.inventory <database>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

QUERIES: dict[str, tuple[str, str]] = {
    "accepted_decisions": ("job_decisions",
                           "SELECT COUNT(*) FROM job_decisions d WHERE d.outcome = 'accepted' "
                           "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = d.job_id)"),
    "accepted_decision_offers": ("job_decisions",
                                 "SELECT COUNT(DISTINCT d.job_id) FROM job_decisions d WHERE d.outcome = 'accepted' "
                                 "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = d.job_id)"),
    "scores": ("job_scores",
               "SELECT COUNT(*) FROM job_scores s WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = s.job_id)"),
    "score_offers": ("job_scores",
                     "SELECT COUNT(DISTINCT s.job_id) FROM job_scores s "
                     "WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = s.job_id)"),
    "feedback_events": ("feedback_events",
                        "SELECT COUNT(*) FROM feedback_events f "
                        "WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = f.job_id)"),
    "exposures": ("exposure_log",
                  "SELECT COUNT(*) FROM exposure_log e WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = e.job_id)"),
}


def inventory(db_path: str | Path) -> dict[str, int | None]:
    """Orphan counts per kind. A table that does not exist yet holds no reference: 0. ``None`` only
    when the database has no ``jobs`` table (nothing to check against)."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out: dict[str, int | None] = {}
        for name, (table, sql) in QUERIES.items():
            if "jobs" not in tables:
                out[name] = None
            else:
                out[name] = int(conn.execute(sql).fetchone()[0]) if table in tables else 0
        return out
    finally:
        conn.close()


def new_orphans(before: dict[str, int | None], after: dict[str, int | None]) -> dict[str, int | None]:
    return {k: (None if before.get(k) is None or after.get(k) is None else int(after[k]) - int(before[k]))  # type: ignore[arg-type]
            for k in QUERIES}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.splitlines()[-1].strip())
    print(json.dumps(inventory(sys.argv[1]), indent=2))
