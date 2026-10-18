"""Issue #68: scheduler retry state machine.

Per schedule, per UTC day (run_once_per_day):

  * nothing today                → dispatch, attempt 1
  * completed today              → nothing more today
  * failed (exception, or did not complete for a reason other than shutdown)
                                 → counts as a failed attempt
  * interrupted (backend shutdown, or recovered as stale at startup)
                                 → not counted; resumed on the next tick
  * skipped (handler was already running)
                                 → not counted, not a failure
  * failed_today <= retry_max and now >= last failure + backoff
                                 → retry, attempt = failed_today + 1
  * failed_today > retry_max     → terminal for today; a new UTC day resets
  * backoff = retry_delay_seconds * 2 ** (failed_today - 1), capped

``retry_max: 0`` therefore means: no automatic retry after a genuine
failure until the next UTC day. The 2026-09-09 (97 LinkedIn re-scrapes) and
2026-09-17 (347 HelloWork failed rows) incidents are the motivation; nothing
here depends on them.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.scheduler.service import (
    MAX_RETRY_BACKOFF_SECONDS,
    DispatchResult,
    HelloWorkHandler,
    LinkedInHandler,
    SchedulerService,
)
from src.scheduler.storage import SchedulerStorage

_DAY = datetime(2026, 9, 18, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Handler:
    """Returns the queued outcomes in order: "completed", "failed",
    "interrupted", "skipped" or "raise"."""

    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def is_running(self) -> bool:
        return False

    def dispatch(self) -> DispatchResult:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "completed"
        if outcome == "raise":
            raise RuntimeError("database is locked")
        if outcome == "skipped":
            return DispatchResult(run_ref=None, completed=False)
        return DispatchResult(
            run_ref=str(uuid.uuid4()),
            completed=outcome == "completed",
            interrupted=outcome == "interrupted",
        )


def _service(tmp_path: Path, handler: Any, clock: _Clock, retry_max: int = 0,
             retry_delay_seconds: int = 300) -> tuple[SchedulerService, str]:
    db_path = str(tmp_path / "sched.db")
    storage = SchedulerStorage(db_path)
    storage.init_db()
    storage.upsert_definition({
        "id": "src_daily", "source_id": "src", "label": "Source daily",
        "enabled": True, "startup_policy": "run_once_per_day",
        "retry_max": retry_max, "retry_delay_seconds": retry_delay_seconds,
    })
    storage.close()
    return SchedulerService(db_path=db_path, config_dir=str(tmp_path),
                            handlers={"src": handler}, clock=clock), db_path


def _tick(service: SchedulerService) -> None:
    """One scheduler tick, waiting for any dispatch it started."""
    service._tick()
    service.join_dispatches(timeout=5)


def _executions(db_path: str) -> list[dict[str, Any]]:
    storage = SchedulerStorage(db_path)
    try:
        return list(reversed(storage.list_executions("src_daily")))  # oldest first
    finally:
        storage.close()


def _statuses(db_path: str) -> list[tuple[str, int]]:
    return [(e["status"], e["attempt"]) for e in _executions(db_path)]


# ---------------------------------------------------------------------------
# retry_max: 0 — no automatic retry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["failed", "raise"])
def test_retry_max_zero_never_retries_the_same_day(tmp_path, failure):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _Handler(failure)
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)
    for minutes in (1, 5, 60, 600):  # every later tick of the same UTC day
        clock.now = _DAY.replace(hour=8) + timedelta(minutes=minutes)
        _tick(service)

    assert handler.calls == 1
    assert _statuses(db) == [("failed", 1)]


def test_a_new_utc_day_resets_the_attempt_budget(tmp_path):
    clock = _Clock(_DAY.replace(hour=23, minute=50))
    handler = _Handler("failed", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)
    clock.now = _DAY + timedelta(days=1, minutes=1)
    _tick(service)

    assert _statuses(db) == [("failed", 1), ("completed", 1)]


# ---------------------------------------------------------------------------
# retry_max > 0 — bounded retries with exponential backoff
# ---------------------------------------------------------------------------

def test_retries_are_bounded_and_backed_off(tmp_path):
    start = _DAY.replace(hour=8)
    clock = _Clock(start)
    handler = _Handler("failed", "failed", "failed", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=2, retry_delay_seconds=300)

    _tick(service)                                   # attempt 1 fails at t0
    t_fail1 = datetime.fromisoformat(_executions(db)[-1]["finished_at"])

    clock.now = t_fail1 + timedelta(seconds=299)
    _tick(service)
    assert handler.calls == 1                        # backoff 300 s not elapsed

    clock.now = t_fail1 + timedelta(seconds=300)
    _tick(service)                                   # attempt 2 fails
    assert _statuses(db) == [("failed", 1), ("failed", 2)]
    t_fail2 = datetime.fromisoformat(_executions(db)[-1]["finished_at"])

    clock.now = t_fail2 + timedelta(seconds=599)
    _tick(service)
    assert handler.calls == 2                        # backoff doubled to 600 s

    clock.now = t_fail2 + timedelta(seconds=600)
    _tick(service)                                   # attempt 3 (last retry) fails
    assert _statuses(db) == [("failed", 1), ("failed", 2), ("failed", 3)]

    clock.now = start + timedelta(hours=12)
    _tick(service)                                   # terminal for today
    assert handler.calls == 3


def test_retry_that_succeeds_ends_the_day(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _Handler("failed", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=1, retry_delay_seconds=60)

    _tick(service)
    clock.now += timedelta(hours=1)
    _tick(service)
    clock.now += timedelta(hours=1)
    _tick(service)

    assert _statuses(db) == [("failed", 1), ("completed", 2)]


def test_backoff_is_capped(tmp_path):
    clock = _Clock(_DAY.replace(hour=0, minute=1))
    handler = _Handler(*(["failed"] * 12))
    service, db = _service(tmp_path, handler, clock, retry_max=20, retry_delay_seconds=3600)

    for _ in range(4):
        _tick(service)
        clock.now = datetime.fromisoformat(_executions(db)[-1]["finished_at"]) + timedelta(
            seconds=MAX_RETRY_BACKOFF_SECONDS
        )
    # 3600 * 2**3 = 8 h would exceed the cap; the cap lets attempt 5 run.
    _tick(service)
    assert handler.calls == 5


def test_terminal_state_is_logged_once(tmp_path, caplog):
    clock = _Clock(_DAY.replace(hour=8))
    service, _db = _service(tmp_path, _Handler("failed"), clock, retry_max=0)
    with caplog.at_level(logging.WARNING, logger="src.scheduler.service"):
        for minutes in range(0, 10):
            clock.now = _DAY.replace(hour=8) + timedelta(minutes=minutes)
            _tick(service)
    terminal = [r for r in caplog.records if "no more automatic attempts today" in r.getMessage()]
    assert len(terminal) == 1


# ---------------------------------------------------------------------------
# Interrupted / skipped are not failures
# ---------------------------------------------------------------------------

def test_interrupted_scan_is_resumed_and_not_counted(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _Handler("interrupted", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)
    clock.now += timedelta(minutes=1)
    _tick(service)

    assert _statuses(db) == [("interrupted", 1), ("completed", 1)]


def test_not_completed_while_the_scheduler_is_stopping_is_interrupted(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    stop_seen = threading.Event()

    class StoppingHandler(_Handler):
        def dispatch(self) -> DispatchResult:
            service.stop()  # backend shutdown while the scan runs
            stop_seen.set()
            return DispatchResult(run_ref="scan-1", completed=False)

    service, db = _service(tmp_path, StoppingHandler(), clock)
    _tick(service)
    assert stop_seen.is_set()
    assert _statuses(db) == [("interrupted", 1)]


def test_already_running_is_skipped_not_failed(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _Handler("skipped", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)
    clock.now += timedelta(minutes=1)
    _tick(service)

    assert _statuses(db) == [("skipped", 1), ("completed", 1)]


def test_stale_running_rows_are_recovered_as_interrupted(tmp_path):
    db_path = str(tmp_path / "sched.db")
    storage = SchedulerStorage(db_path)
    storage.init_db()
    exec_id = storage.try_reserve_execution("src_daily", trigger="startup")
    assert exec_id is not None
    assert storage.recover_stale_executions() == 1
    row = storage.list_executions("src_daily")[0]
    storage.close()
    assert row["status"] == "interrupted"
    assert "backend stopped" in row["error"]


# ---------------------------------------------------------------------------
# Manual trigger and handlers
# ---------------------------------------------------------------------------

def test_manual_trigger_bypasses_the_retry_budget(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _Handler("failed", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)
    clock.now += timedelta(minutes=1)
    service.trigger_manual("src_daily")
    service.join_dispatches(timeout=5)

    assert _statuses(db) == [("failed", 1), ("completed", 2)]


@pytest.mark.parametrize("handler_cls", [LinkedInHandler, HelloWorkHandler])
@pytest.mark.parametrize("shutdown", [True, False])
def test_handlers_report_a_shutdown_stop_as_interrupted(handler_cls, shutdown):
    class _Scanner:
        def run_scan(self) -> str:
            return "scan-1"

        def last_scan_completed(self) -> bool:
            return False

        def shutdown_requested(self) -> bool:
            return shutdown

    result = handler_cls(scanner=_Scanner()).dispatch()
    assert result == DispatchResult(run_ref="scan-1", completed=False, interrupted=shutdown)
