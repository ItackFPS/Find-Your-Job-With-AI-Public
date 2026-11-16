"""What a run achieved, in one word the screens share (Phase B B7-A, open question OQ-3).

| Outcome | Definition | Visible list |
|---|---|---|
| ``successful`` | every source that ran is complete, new offers stored | this run |
| ``partial`` | at least one source succeeded, at least one is incomplete | this run; the incomplete sources are named |
| ``no_new_offers`` | every source that ran is complete; no offer newly stored | this run; "nothing new" |
| ``no_successful_source`` | every source that ran is incomplete, or none could run | the previous list is kept |
| ``failed`` | the pipeline itself failed | the previous list is kept |
| ``first_run_empty`` | no earlier visible run and no offer accepted | an empty list that says why |

A source is **incomplete** when it failed, came back partial, reported a network outage, or is
**suspect**: it completed with no offer although it returned offers before (``suspect_empty``, set by
the orchestrator). A source that did not run (``skipped`` by the preflight or for having nothing to
search, ``paused`` by its breaker) does not count either way.

Since issue #253 every run also carries the stored active offers of every enabled source that it did
not collect (``src/pipeline/carryover.py``), so "this run" no longer means "only what was collected":
an incomplete or empty source never takes its offers out of the list. The outcome says what the
collection achieved; it no longer decides what the list loses.
"""

from __future__ import annotations

from typing import Any

SUCCESSFUL = "successful"
PARTIAL = "partial"
NO_NEW_OFFERS = "no_new_offers"
NO_SUCCESSFUL_SOURCE = "no_successful_source"
FAILED = "failed"
FIRST_RUN_EMPTY = "first_run_empty"

# Outcomes whose run does not replace the visible list.
KEEPS_PREVIOUS_LIST = frozenset({NO_SUCCESSFUL_SOURCE, FAILED})

_RAN = ("completed", "partial", "failed")


def incomplete_reason(row: dict[str, Any]) -> str | None:
    """Why a source that ran is incomplete (``failed``, ``partial``, ``network_outage``,
    ``suspect_empty``), or ``None`` when it ran fine or did not run."""
    status = row.get("status")
    if status not in _RAN:
        return None
    if row.get("network_outage"):
        return "network_outage"
    if status in ("failed", "partial"):
        return str(status)
    if row.get("suspect_empty"):
        return "suspect_empty"
    return None


def incomplete_sources(by_source: dict[str, Any] | None) -> dict[str, str]:
    """The sources that ran and are incomplete, by name, with the reason (issue #253: named)."""
    out: dict[str, str] = {}
    for name, row in sorted((by_source or {}).items()):
        if isinstance(row, dict):
            reason = incomplete_reason(row)
            if reason:
                out[name] = reason
    return out


def run_outcome(status: str, by_source: dict[str, Any] | None, *, accepted: int,
                stored_new: int | None, has_previous_list: bool) -> str:
    """The outcome of one run. ``stored_new`` is the number of offers stored for the first time
    (``None`` when unknown, then "nothing new" is never claimed)."""
    if status != "completed":
        return FAILED
    rows = [r for r in (by_source or {}).values() if isinstance(r, dict)]
    ran = [r for r in rows if r.get("status") in _RAN]
    incomplete = [r for r in ran if incomplete_reason(r)]
    # A partial source still brought offers: it counts as succeeded, and makes the run partial.
    succeeded = [r for r in ran if r.get("status") in ("completed", "partial")
                 and not r.get("network_outage") and not r.get("suspect_empty")]
    if not succeeded:
        return NO_SUCCESSFUL_SOURCE
    if not has_previous_list and accepted == 0:
        return FIRST_RUN_EMPTY
    if incomplete:
        return PARTIAL
    if stored_new == 0:
        return NO_NEW_OFFERS
    return SUCCESSFUL


def failed_sources(by_source: dict[str, Any] | None) -> list[str]:
    """The sources that ran and failed or came back partial, by name (OQ-3: they are named)."""
    return sorted(name for name, r in (by_source or {}).items()
                  if isinstance(r, dict) and r.get("status") in ("failed", "partial"))
