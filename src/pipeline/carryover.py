"""Carry-forward of stored offers into every run (issue #253, owner decision 2026-11-16).

The main list shows the offers scored in the latest visible run. Before this module a run scored
only what it collected, plus the stored offers LinkedIn and HelloWork re-injected; a source that
returned nothing, failed or was skipped took its still-active offers out of the list without notice
(B11 finding 4: 797 -> 273). Now every run also scores the stored ``active`` offers of every source
that is on (:func:`src.source_enablement.enabled_sources`) that it did not collect: the "carried"
offers. They are re-filtered and re-scored like any other offer, so the profile still decides.

A carried offer was not seen: it does not reset its miss streak, does not refresh its provenance
(``last_seen_at``) and is not written back. It leaves the list only by an explicit rule: expiry
(end date, missed complete listings, or ``expire_after_days_unseen`` days without being collected),
its source turned off, the user hiding it, or the profile rejecting it.

This module also marks a source **suspect** (``suspect_empty``) when it completed with no offer
although it returned offers the last time it ran: such a run is named as incomplete
(``src/pipeline/run_outcome.py``) instead of claiming "nothing new".
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.models.job import Job
    from src.storage.sqlite import Storage

logger = logging.getLogger(__name__)


def load_carryover(storage: Storage, config_dir: str | Path, settings_path: str | Path | None,
                   exclude_ids: set[str], stale_before: datetime | None = None,
                   ) -> tuple[list[Job], dict[str, int]]:
    """The stored active offers of every enabled source whose id is not in ``exclude_ids`` (the ids
    this run collected), and their count per source. Sources in a stable (sorted) order.

    ``stale_before``: an offer last collected before it is not carried; the run's expiry marks it
    ``stale`` (``Storage.apply_expiry``), so it never shows in this run's list."""
    from src.source_enablement import enabled_sources

    jobs: list[Job] = []
    for source in sorted(enabled_sources(config_dir, settings_path)):
        jobs.extend(storage.get_active_jobs_by_source(source, exclude_ids=exclude_ids))
    if stale_before is not None and jobs:
        stale = _last_collected_before(storage, [j.id for j in jobs], stale_before)
        jobs = [j for j in jobs if j.id not in stale]
    return jobs, dict(sorted(Counter(j.source for j in jobs).items()))


def _last_collected_before(storage: Storage, ids: list[str], when: datetime) -> set[str]:
    """The ids whose last collection (latest provenance sighting, else ``discovered_at``) is older
    than ``when``: the same rule as the ``stale`` expiry."""
    conn = storage._get_conn()  # noqa: SLF001
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = conn.execute(
            f"""SELECT j.id FROM jobs j WHERE j.id IN ({','.join('?' * len(chunk))})
                AND COALESCE((SELECT MAX(p.last_seen_at) FROM job_provenance p WHERE p.job_id = j.id),
                             j.discovered_at) < ?""",
            [*chunk, when.isoformat()]).fetchall()
        out |= {r[0] for r in rows}
    return out


def _previous_yield(history: list[dict[str, Any]], source: str) -> int | None:
    """What ``source`` collected the last time it ran (completed or partial), most recent first."""
    for run in history:
        row = ((run.get("stats") or {}).get("by_source") or {}).get(source)
        if isinstance(row, dict) and row.get("status") in ("completed", "partial"):
            collected = row.get("collected")
            if isinstance(collected, int) and not isinstance(collected, bool):
                return collected
    return None


def mark_suspect_empty(by_source: dict[str, dict[str, Any]], history: list[dict[str, Any]]) -> list[str]:
    """Set ``suspect_empty`` on each row that completed with no offer although the source returned
    offers the last time it ran (or whose yield anomaly says it dropped to 0). ``history``: earlier
    runs, most recent first, without the current one. Returns the names marked."""
    marked = []
    for name, row in by_source.items():
        if not isinstance(row, dict) or row.get("status") != "completed" or row.get("from_staging"):
            continue
        if (row.get("collected") or 0) > 0:
            continue
        previous = _previous_yield(history, name)
        anomaly = row.get("yield_anomaly") or {}
        if (previous or 0) > 0 or anomaly.get("current_yield") == 0:
            row["suspect_empty"] = True
            marked.append(name)
    return sorted(marked)
