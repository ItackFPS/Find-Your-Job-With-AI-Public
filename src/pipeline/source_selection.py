"""Which configured sources run in one pipeline run, and what happens to their circuit breakers.

``select_sources`` decides, before collection, which active sources run and builds the
``by_source`` row of every active source that does not: removed in 2.0 (a 1.x config may still
list it), left out by the preflight (a credential is missing), or paused by its circuit breaker.
``settle_breakers`` runs after collection: each source that ran moves its breaker, unless the
whole run looks like this machine's network outage.

Neither function raises for a source problem: a source that cannot run is a row with a reason,
never a failed run (ADR 0001: sources and optional stages fail open).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.collectors.registry import REMOVED_SOURCE_TYPES
from src.pipeline.circuit_breaker import (
    BreakerState,
    is_network_outage,
    load_states,
    next_state,
    paused_reason,
    save_states,
)

logger = logging.getLogger(__name__)

NETWORK_OUTAGE_ERROR = (
    "No source could be reached (network down?): failed sources are not counted "
    "towards their circuit breaker this run"
)


@dataclass
class SourceSelection:
    """The sources that run, the rows of the active sources that do not, and the breakers read."""

    runnable: list[dict[str, Any]]
    not_run_rows: dict[str, dict[str, Any]]
    breakers: dict[str, BreakerState]
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


def _not_run_row(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"collected": 0, "accepted": 0, "errors": 0, "status": status, "reason": reason, **extra}


def select_sources(
    all_sources: list[dict[str, Any]],
    *,
    skipped_types: set[str],
    skip_reasons: dict[str, str],
    db_path: str,
) -> SourceSelection:
    """Split the configured sources into those that run and the rows of those that do not.

    Rows are built in this order: removed types, preflight skips, breaker pauses.
    """
    active = [s for s in all_sources if s.get("active", True)]
    rows: dict[str, dict[str, Any]] = {}

    # A 1.x config may still list a source type removed in 2.0: skip it with one line per run
    # saying so, rather than a failure on every run.
    for s in active:
        removed_reason = REMOVED_SOURCE_TYPES.get(s.get("type", ""))
        if removed_reason:
            logger.warning("Skipping source %s: %s", s["name"], removed_reason)
            rows[s["name"]] = _not_run_row("skipped", removed_reason)
    # An active source left out by the preflight is listed as skipped, with what to set up,
    # so a missing credential never looks like a source that ran.
    for s in active:
        if s.get("type") in skipped_types:
            rows[s["name"]] = _not_run_row("skipped", skip_reasons.get(s.get("type", ""), ""))

    runnable = [s for s in active if s.get("type") not in skipped_types
                and s.get("type") not in REMOVED_SOURCE_TYPES]

    # Circuit breaker (plan F6.1): a source that failed FAILURE_THRESHOLD runs in a row is
    # skipped until its pause is over, then tried again. Never blocks the run.
    selection = SourceSelection(runnable=runnable, not_run_rows=rows, breakers=load_states(db_path))
    paused = {s["name"] for s in runnable
              if s["name"] in selection.breakers and selection.breakers[s["name"]].is_paused(selection.now)}
    for name in sorted(paused):
        state = selection.breakers[name]
        reason = paused_reason(state)
        logger.warning("Skipping source %s: %s", name, reason)
        rows[name] = _not_run_row(
            "paused", reason,
            paused_until=state.paused_until.isoformat() if state.paused_until else None,
            consecutive_failures=state.consecutive_failures)
    selection.runnable = [s for s in runnable if s["name"] not in paused]
    return selection


def settle_breakers(selection: SourceSelection, by_source: dict[str, dict[str, Any]], db_path: str) -> list[str]:
    """Move the breaker of every source that ran; return the run-level errors to record.

    A success closes a breaker, a failure counts, the third failure in a row pauses the source
    (its row says until when). A run where (nearly) every source got no HTTP answer at all is this
    machine's outage, not the sources' failure: no breaker moves and the rows say so.
    """
    errors: list[str] = []
    ran_rows = [by_source.get(cfg["name"], {}) for cfg in selection.runnable]
    if is_network_outage(ran_rows):
        for row in ran_rows:
            row["network_outage"] = True
        errors.append(NETWORK_OUTAGE_ERROR)
        counted: list[dict[str, Any]] = []
    else:
        counted = selection.runnable
    updated: list[BreakerState] = []
    for cfg in counted:
        name = cfg["name"]
        row = by_source.get(name, {})
        state = next_state(selection.breakers.get(name) or BreakerState(source=name),
                           row.get("status"), row.get("reason"), selection.now)
        if state.consecutive_failures:
            row["consecutive_failures"] = state.consecutive_failures
        if state.paused_until is not None:
            row["paused_until"] = state.paused_until.isoformat()
        if name in selection.breakers or state.consecutive_failures:
            updated.append(state)
    save_states(db_path, updated)
    return errors
