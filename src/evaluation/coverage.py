"""France Travail web coverage (plan F0.3, completion plan P2.7): how many API offers the web collector finds.

The web collector reads the public site; the API needs credentials. Both name an offer by France Travail's
own offer number (``source_job_id``), so the two can be matched in ``job_provenance`` whatever deduplication
did with the rows. Over a window (7 days by default), coverage is the share of offer numbers the API saw
that the web collector also saw in the same window. Both collectors run with the same profile, so they
query the same occupations and départements.

Read-only (``mode=ro``, ``query_only``). The result is a status and counts only, never offer content:

* ``no_api_offers`` — the API saw nothing in the window (no credentials, or not running);
* ``no_web_offers`` — the web collector saw nothing in the window;
* ``too_early`` — both ran, but on fewer than ``window_days`` distinct days: the share is shown, not final;
* ``measured`` — both ran on at least ``window_days`` distinct days of the window.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.evaluation.export import open_readonly
from src.evaluation.sampling import wilson

API_SOURCE = "france_travail"
WEB_SOURCE = "france-travail-web"
WINDOW_DAYS = 7


def _seen(conn: Any, source: str, start: str) -> tuple[set[str], set[str]]:
    """Offer numbers ``source`` saw since ``start``, and the distinct days it saw any."""
    rows = conn.execute(
        "SELECT source_job_id, substr(last_seen_at, 1, 10) AS day FROM job_provenance "
        "WHERE source = ? AND last_seen_at >= ? AND source_job_id IS NOT NULL AND source_job_id != ''",
        (source, start)).fetchall()
    return {r["source_job_id"] for r in rows}, {r["day"] for r in rows}


def ft_web_coverage(db_path: str | Path, *, now: datetime | None = None, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    start = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    conn = open_readonly(db_path)
    try:
        api, api_days = _seen(conn, API_SOURCE, start)
        web, web_days = _seen(conn, WEB_SOURCE, start)
    finally:
        conn.close()
    found = len(api & web)
    days = len(api_days & web_days)
    if not api:
        status = "no_api_offers"
    elif not web:
        status = "no_web_offers"
    else:
        status = "measured" if days >= window_days else "too_early"
    return {
        "status": status, "window_days": window_days, "window_start": start, "days_with_both": days,
        "api_offers": len(api), "web_offers": len(web), "found_by_web": found,
        "share": found / len(api) if api else None, "ci95": wilson(found, len(api)),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m src.evaluation.coverage",
                                 description="France Travail web coverage of the API offers (read-only)")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ns = ap.parse_args(argv)
    print(json.dumps(ft_web_coverage(ns.db, window_days=ns.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
