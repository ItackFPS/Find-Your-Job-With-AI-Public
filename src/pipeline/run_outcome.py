"""What a run achieved, in one word the screens share (Phase B B7-A, open question OQ-3).

| Outcome | Definition | Visible list |
|---|---|---|
| ``successful`` | every source that ran completed, new offers stored | this run |
| ``partial`` | at least one source completed, at least one failed or partial | this run; the failed sources are named |
| ``no_new_offers`` | sources completed; no offer newly stored | this run (the same offers); "nothing new" |
| ``no_successful_source`` | every source that ran failed or reported an outage, or none could run | the previous list is kept |
| ``failed`` | the pipeline itself failed | the previous list is kept |
| ``first_run_empty`` | no earlier visible run and no offer accepted | an empty list that says why |

A source that did not run (``skipped`` by the preflight or for having nothing to search, ``paused``
by its breaker) does not count either way.
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


def run_outcome(status: str, by_source: dict[str, Any] | None, *, accepted: int,
                stored_new: int | None, has_previous_list: bool) -> str:
    """The outcome of one run. ``stored_new`` is the number of offers stored for the first time
    (``None`` when unknown, then "nothing new" is never claimed)."""
    if status != "completed":
        return FAILED
    rows = [r for r in (by_source or {}).values() if isinstance(r, dict)]
    ran = [r for r in rows if r.get("status") in _RAN]
    succeeded = [r for r in ran if r.get("status") in ("completed", "partial") and not r.get("network_outage")]
    if not succeeded:
        return NO_SUCCESSFUL_SOURCE
    if not has_previous_list and accepted == 0:
        return FIRST_RUN_EMPTY
    if any(r.get("status") in ("failed", "partial") for r in ran):
        return PARTIAL
    if stored_new == 0:
        return NO_NEW_OFFERS
    return SUCCESSFUL


def failed_sources(by_source: dict[str, Any] | None) -> list[str]:
    """The sources that ran and failed or came back partial, by name (OQ-3: they are named)."""
    return sorted(name for name, r in (by_source or {}).items()
                  if isinstance(r, dict) and r.get("status") in ("failed", "partial"))
