"""Per-source circuit breaker (plan §C1 "Failures", F6.1).

After ``FAILURE_THRESHOLD`` consecutive failed runs a source is paused: the next runs
skip it (``status: "paused"`` in the run's ``by_source`` row, with the reason and
``paused_until``) instead of hitting a broken or blocking site again. The pause doubles
on every further trip (``BASE_PAUSE`` x 2^(trips-1), at most ``MAX_PAUSE``). When the
pause is over the source runs again: one success closes the breaker (counters reset);
one more failure pauses it again, for longer.

A run counts as failed when its collection status is ``failed`` (errors and nothing
collected; the parser canary's ``parser_changed`` is one). ``partial`` and
``completed`` are successes; ``skipped`` and ``paused`` runs change nothing. A run where
(nearly) every source got no HTTP answer at all (``is_network_outage``: the machine is
offline, DNS is down) changes nothing either. ``reset`` closes a breaker by hand (the
``POST /api/sources/{name}/breaker/reset`` route).

State lives in the ``source_breakers`` table (src/storage/runs.py). Reading or writing
it never breaks a run: on any storage error the run proceeds as if no source were paused.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
BASE_PAUSE = timedelta(hours=6)
MAX_PAUSE = timedelta(days=7)
# Share of the sources that ran which must have got no HTTP answer at all for the run to
# count as a network outage (with at least two sources run and none reachable).
OUTAGE_SHARE = 0.8


@dataclass(frozen=True)
class BreakerState:
    source: str
    consecutive_failures: int = 0
    trips: int = 0
    paused_until: datetime | None = None
    last_failure_reason: str | None = None
    last_failure_at: datetime | None = None

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < self.paused_until

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("paused_until", "last_failure_at"):
            row[key] = row[key].isoformat() if row[key] is not None else None
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> BreakerState:
        def when(value: Any) -> datetime | None:
            try:
                return datetime.fromisoformat(value) if value else None
            except (TypeError, ValueError):
                return None

        return cls(source=str(row["source"]), consecutive_failures=int(row.get("consecutive_failures") or 0),
                   trips=int(row.get("trips") or 0), paused_until=when(row.get("paused_until")),
                   last_failure_reason=row.get("last_failure_reason"), last_failure_at=when(row.get("last_failure_at")))


def pause_duration(trips: int) -> timedelta:
    """How long the ``trips``-th pause of a failure streak lasts."""
    pause: timedelta = BASE_PAUSE * (2 ** max(trips - 1, 0))
    return min(pause, MAX_PAUSE)


def next_state(state: BreakerState, status: str | None, reason: str | None, now: datetime) -> BreakerState:
    """The breaker after one run of the source ended with collection ``status``."""
    if status in ("completed", "partial"):
        return BreakerState(source=state.source)
    if status != "failed":
        return state
    failures = state.consecutive_failures + 1
    new = replace(state, consecutive_failures=failures, last_failure_reason=reason, last_failure_at=now)
    if failures >= FAILURE_THRESHOLD:
        trips = state.trips + 1
        new = replace(new, trips=trips, paused_until=now + pause_duration(trips))
    return new


def is_network_outage(rows: list[dict[str, Any]]) -> bool:
    """Whether a run's per-source rows show this machine's outage rather than sources
    failing: at least two sources ran, none was reached, and at least ``OUTAGE_SHARE`` of
    them failed without any HTTP answer (``network_unreachable``)."""
    ran = [r for r in rows if r.get("status") in ("completed", "partial", "failed")]
    if len(ran) < 2:
        return False
    if any(r.get("status") in ("completed", "partial") and not r.get("network_unreachable") for r in ran):
        return False
    unreachable = sum(1 for r in ran if r.get("status") == "failed" and r.get("network_unreachable"))
    return unreachable >= OUTAGE_SHARE * len(ran)


def paused_reason(state: BreakerState) -> str:
    until = state.paused_until.isoformat(timespec="minutes") if state.paused_until else "?"
    last = f" (last: {state.last_failure_reason})" if state.last_failure_reason else ""
    return (f"paused after {state.consecutive_failures} failed runs in a row{last}; "
            f"tried again automatically after {until}")


def load_states(db_path: str) -> dict[str, BreakerState]:
    """Every source's breaker, by source name; ``{}`` when the store cannot be read."""
    from src.storage.runs import RunStorage

    try:
        storage = RunStorage(db_path=db_path)
        storage.init_db()
        try:
            return {row["source"]: BreakerState.from_row(row) for row in storage.list_source_breakers()}
        finally:
            storage.close()
    except Exception as exc:  # noqa: BLE001 - a breaker must never break a run
        logger.warning("Circuit breaker state unreadable (no source paused this run): %s", exc)
        return {}


def save_states(db_path: str, states: list[BreakerState]) -> None:
    from src.storage.runs import RunStorage

    try:
        storage = RunStorage(db_path=db_path)
        storage.init_db()
        try:
            storage.save_source_breakers([s.to_row() for s in states])
        finally:
            storage.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Circuit breaker state not saved: %s", exc)


def reset(db_path: str, source: str) -> BreakerState | None:
    """Close ``source``'s breaker by hand (counters cleared, pause lifted): the next run
    tries it. ``None`` when the source has no breaker row. Raises on storage errors."""
    from src.storage.runs import RunStorage

    storage = RunStorage(db_path=db_path)
    storage.init_db()
    try:
        if not any(row["source"] == source for row in storage.list_source_breakers()):
            return None
        state = BreakerState(source=source)
        storage.save_source_breakers([state.to_row()])
        return state
    finally:
        storage.close()
