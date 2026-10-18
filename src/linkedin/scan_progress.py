"""Work-based progress and ETA for a LinkedIn scan (issue #86).

Pure functions over the resumable checkpoint, the scanner config and past
completed scan runs — no I/O, so the status API and tests share one
calculation.

The unit of work is a result page. How many pages a query has is only known
once it is exhausted, so each query's page count is *estimated*:

  1. its pages_fetched in the most recent completed run where it actually
     ran (exhausted or hit the safety limit — not skipped, not failed);
  2. otherwise the median of the other queries' known counts;
  3. otherwise max_pages_per_query.

A completed query counts its actual pages when the checkpoint recorded them,
and the in-progress query's estimate grows past its history as needed, so it
never reads as complete early. Progress is the page-weighted share of work
done — stable across process restarts because it depends only on persisted
state, never on when the current process started.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
from datetime import datetime
from typing import Any

# Queries a completed run's stats report under these stopped_by values
# genuinely ran to their end, so their pages_fetched is a real page count.
_RAN_TO_END = ("exhaustion", "safety_limit")

# A rate from fewer pages than this is too noisy to project an ETA from.
MIN_PAGES_FOR_RATE = 3


def _query_key(search_term: str, location: str | None) -> str:
    """Same key format as src/linkedin/scanner.py::_query_key, rebuilt from
    the search_term/location a run's stats record per query."""
    return f"{search_term}\x1f{location or ''}"


def _run_stats(run: dict[str, Any]) -> dict[str, Any]:
    try:
        stats = json.loads(run.get("stats_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return stats if isinstance(stats, dict) else {}


def _completed(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in runs if r.get("status") == "completed"]


def expected_pages(
    query_keys: list[str], runs: list[dict[str, Any]], max_pages: int
) -> dict[str, int]:
    """Estimated page count per query key; ``runs`` are newest first."""
    known: dict[str, int] = {}
    for run in _completed(runs):
        for q in _run_stats(run).get("queries") or []:
            if not isinstance(q, dict) or q.get("stopped_by") not in _RAN_TO_END:
                continue
            key = _query_key(str(q.get("search_term") or ""), q.get("location"))
            known.setdefault(key, int(q.get("pages_fetched") or 0))
    in_config = [known[k] for k in query_keys if k in known]
    fallback = math.ceil(statistics.median(in_config)) if in_config else max_pages
    return {k: min(known.get(k, fallback), max_pages) for k in query_keys}


def estimate_progress(
    query_keys: list[str],
    completed_keys: set[str],
    current_key: str | None,
    current_page: int,
    expected: dict[str, int],
    actual_pages: dict[str, int],
) -> dict[str, Any]:
    """Page-weighted progress. ``current_page`` is the next page to fetch,
    i.e. the number of pages of the in-progress query already done."""
    done = total = 0
    current_est: int | None = None
    for key in query_keys:
        if key in completed_keys:
            pages = actual_pages.get(key, expected[key])
            done += pages
            total += pages
        elif key == current_key:
            current_est = max(expected[key], current_page + 1)
            done += current_page
            total += current_est
        else:
            total += expected[key]
    if query_keys and completed_keys >= set(query_keys):
        percent = 100
    elif total <= 0:
        percent = 0
    else:
        # Below 100 until every query is complete: estimates can undershoot.
        percent = max(0, min(99, math.floor(100 * done / total)))
    return {
        "percent": percent,
        "pages_done_est": done,
        "pages_total_est": total,
        "current_query_pages_est": current_est,
        "estimated": True,
    }


def _history_seconds_per_page(runs: list[dict[str, Any]]) -> float | None:
    """Median seconds/page over completed runs. Uses active_seconds when the
    run recorded it; otherwise the wall-clock duration, but only for a run
    that never resumed (its duration would include downtime)."""
    rates: list[float] = []
    for run in _completed(runs):
        stats = _run_stats(run)
        pages = int(stats.get("total_pages_fetched") or 0)
        if pages < MIN_PAGES_FOR_RATE:
            continue
        seconds = stats.get("active_seconds")
        if seconds is None:
            queries = stats.get("queries") or []
            if any(isinstance(q, dict) and q.get("stopped_by") == "resumed_skip" for q in queries):
                continue
            try:
                seconds = (
                    datetime.fromisoformat(run["completed_at"])
                    - datetime.fromisoformat(run["started_at"])
                ).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
        if seconds and seconds > 0:
            rates.append(float(seconds) / pages)
    return statistics.median(rates) if rates else None


def seconds_per_page(
    attempt_seconds: float,
    attempt_pages: int,
    checkpoint_stats: dict[str, Any],
    runs: list[dict[str, Any]],
) -> tuple[float | None, str | None]:
    """(seconds per page, basis) — basis is 'live' (this process, ≥3 pages),
    'checkpoint' (every attempt of this logical scan), 'history' (median of
    completed runs), or (None, None) when there is not enough data."""
    if attempt_pages >= MIN_PAGES_FOR_RATE and attempt_seconds > 0:
        return attempt_seconds / attempt_pages, "live"
    queries = checkpoint_stats.get("queries") or {}
    pages = sum(int(q.get("pages_fetched") or 0) for q in queries.values() if isinstance(q, dict))
    active = float(checkpoint_stats.get("active_seconds") or 0.0)
    if pages >= MIN_PAGES_FOR_RATE and active > 0:
        return active / pages, "checkpoint"
    history = _history_seconds_per_page(runs)
    if history is not None:
        return history, "history"
    return None, None


def eta_seconds(progress: dict[str, Any], rate: float | None) -> int | None:
    """Remaining estimated pages × seconds per page; None without a rate."""
    if rate is None:
        return None
    remaining = max(0, int(progress["pages_total_est"]) - int(progress["pages_done_est"]))
    return round(remaining * rate)
