"""``python -m src.feedback.cli`` — read-only status inspection and one-time backfill."""

from __future__ import annotations

import argparse
import sqlite3
import sys


def inspect_statuses(db_path: str) -> str:
    """READ-ONLY report of the application_status values and transitions actually in the database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    out = ["application_status values in jobs:"]
    try:
        for status, n in conn.execute(
            "SELECT application_status, COUNT(*) FROM jobs WHERE application_status IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        ):
            out.append(f"  {status!r:>12}  {n}")
        out.append("transitions recorded in outcome_events (from -> to):")
        for frm, to, n in conn.execute(
            "SELECT json_extract(metadata,'$.from'), json_extract(metadata,'$.to'), COUNT(*) "
            "FROM outcome_events WHERE event_type='application_status_changed' GROUP BY 1,2 ORDER BY 3 DESC"
        ):
            out.append(f"  {frm!r:>12} -> {to!r:<12} {n}")
    except sqlite3.OperationalError as exc:
        out.append(f"  (not available: {exc})")
    finally:
        conn.close()
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.feedback.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inspect-statuses", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--db", default="data/jobs.db")
        p.add_argument("--config-dir", default="config")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "inspect-statuses":
            print(inspect_statuses(ns.db))
        else:
            from src.feedback.service import FeedbackService

            done, skipped = FeedbackService(ns.db, ns.config_dir).backfill_from_outcome_events()
            print(f"backfilled {done} event(s); skipped {skipped} malformed")
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
