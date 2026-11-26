"""Issue #81: scheduler behaviour around user pause / resume.

  * a paused source is skipped (no execution row) until it is resumed
  * a run stopped by a pause is recorded 'interrupted' — not a failure
  * resume_source() goes through the schedule, so a resumed scan counts as
    today's run and no second full scan follows
  * a checkpoint left 'interrupted' by a later (manual) scan is resumed even
    when today's scheduled run already completed; 'failed' ones are not
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from src.scheduler.service import DispatchResult, HelloWorkHandler
from tests.test_scheduler_retry_policy import _DAY, _Clock, _executions, _service, _statuses, _tick


class _PausableHandler:
    """A handler whose pause / interrupted-checkpoint flags are set by the test."""

    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.paused = False
        self.interrupted_checkpoint = False
        # Bumped whenever the test simulates a fresh interruption, so the
        # checkpoint identity changes like a real re-written checkpoint.
        self.checkpoint_version = 0

    def is_running(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return self.paused

    def interrupted_checkpoint_id(self) -> str | None:
        return f"cp-{self.checkpoint_version}" if self.interrupted_checkpoint else None

    def dispatch(self) -> DispatchResult:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "completed"
        if outcome == "paused":
            self.paused = True
        if outcome in ("completed", "paused", "failed"):
            self.interrupted_checkpoint = False
        return DispatchResult(
            run_ref=str(uuid.uuid4()),
            completed=outcome == "completed",
            paused=outcome == "paused",
        )


def test_paused_source_is_skipped_without_an_execution_row(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _PausableHandler()
    handler.paused = True
    service, db = _service(tmp_path, handler, clock)

    for minutes in (0, 1, 60, 24 * 60):  # including the next UTC day
        clock.now = _DAY.replace(hour=8) + timedelta(minutes=minutes)
        _tick(service)

    assert handler.calls == 0
    assert _executions(db) == []


def test_paused_run_is_interrupted_not_failed_and_not_retried_until_resumed(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _PausableHandler("paused", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)
    assert _statuses(db) == [("interrupted", 1)]
    assert "paused by user" in _executions(db)[0]["error"]

    clock.now += timedelta(minutes=5)
    _tick(service)
    assert handler.calls == 1  # still paused: nothing dispatched

    # The user resumes: the pause no longer counts against retry_max=0.
    assert service.resume_source("src") is not None
    service.join_dispatches(timeout=5)
    assert _statuses(db) == [("interrupted", 1), ("completed", 1)]
    assert _executions(db)[-1]["trigger"] == "resume"

    clock.now += timedelta(minutes=5)
    _tick(service)
    assert handler.calls == 2  # today is done — no duplicate full scan


def test_resume_source_without_an_enabled_schedule_returns_none(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    service, _db = _service(tmp_path, _PausableHandler(), clock)
    assert service.resume_source("unknown-source") is None


def test_resume_source_while_running_raises(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _PausableHandler()
    handler.is_running = lambda: True  # type: ignore[method-assign]
    service, _db = _service(tmp_path, handler, clock)
    with pytest.raises(RuntimeError):
        service.resume_source("src")


def test_interrupted_checkpoint_is_resumed_even_when_today_is_completed(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _PausableHandler("completed", "completed")
    service, db = _service(tmp_path, handler, clock)

    _tick(service)  # the scheduled run completes
    # Later, a manual scan is cut off by a shutdown/crash.
    handler.interrupted_checkpoint = True
    clock.now += timedelta(hours=2)
    _tick(service)

    assert _statuses(db) == [("completed", 1), ("completed", 1)]
    assert _executions(db)[-1]["trigger"] == "resume"

    clock.now += timedelta(minutes=5)
    _tick(service)
    assert handler.calls == 2  # checkpoint consumed — no further dispatch


def test_failed_checkpoint_is_not_auto_resumed_after_completion(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _PausableHandler("completed")
    service, _db = _service(tmp_path, handler, clock)

    _tick(service)
    clock.now += timedelta(hours=2)
    _tick(service)  # interrupted_checkpoint stays False (a 'failed' checkpoint)
    assert handler.calls == 1


def test_mock_handlers_are_never_read_as_paused(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = MagicMock()
    handler.is_running.return_value = False
    handler.dispatch.return_value = DispatchResult(run_ref="r", completed=True)
    service, db = _service(tmp_path, handler, clock)
    _tick(service)
    assert _statuses(db) == [("completed", 1)]


@pytest.mark.parametrize("handler_cls", [HelloWorkHandler])
def test_handlers_report_a_pause_stop(handler_cls):
    class _Scanner:
        def run_scan(self) -> str:
            return "scan-1"

        def last_scan_completed(self) -> bool:
            return False

        def last_scan_paused(self) -> bool:
            return True

        def shutdown_requested(self) -> bool:
            return False

        def is_paused(self) -> bool:
            return True

        def interrupted_checkpoint_id(self) -> str | None:
            return None

    handler = handler_cls(scanner=_Scanner())
    assert handler.dispatch() == DispatchResult(run_ref="scan-1", completed=False, paused=True)
    assert handler.is_paused() is True
    assert handler.interrupted_checkpoint_id() is None


def test_interrupted_checkpoint_is_resumed_on_a_retry_exhausted_day(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _PausableHandler("failed", "completed")
    service, db = _service(tmp_path, handler, clock, retry_max=0)

    _tick(service)  # the scheduled run fails: retry budget spent for today
    # A later manual scan is cut off by a shutdown/crash.
    handler.interrupted_checkpoint = True
    clock.now += timedelta(hours=1)
    _tick(service)

    assert _statuses(db) == [("failed", 1), ("completed", 2)]
    assert _executions(db)[-1]["trigger"] == "resume"


# ---------------------------------------------------------------------------
# Review follow-ups (issue #81): bounded auto-resume (F1) and a failed
# 'failed'-label write not bypassing the retry policy (F2).
# ---------------------------------------------------------------------------

class _StickyCheckpointHandler(_PausableHandler):
    """A resumed scan that completes but leaves its checkpoint untouched."""

    def dispatch(self) -> DispatchResult:
        self.calls += 1
        return DispatchResult(run_ref=str(uuid.uuid4()), completed=True)


def test_an_interrupted_checkpoint_is_auto_resumed_once_not_every_tick(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _StickyCheckpointHandler()
    service, db = _service(tmp_path, handler, clock)

    _tick(service)  # today's scheduled run completes
    handler.interrupted_checkpoint = True  # a later scan was interrupted
    for minutes in range(1, 6):
        clock.now = _DAY.replace(hour=8) + timedelta(minutes=minutes)
        _tick(service)

    assert [(e["status"], e["trigger"]) for e in _executions(db)] == [
        ("completed", "scheduled"), ("completed", "resume"),
    ]

    # A genuinely new interruption (the checkpoint was written again) is resumed.
    handler.checkpoint_version += 1
    clock.now += timedelta(minutes=1)
    _tick(service)
    clock.now += timedelta(minutes=1)
    _tick(service)
    assert [e["trigger"] for e in _executions(db)] == ["scheduled", "resume", "resume"]


def test_manual_resume_of_an_unchanged_checkpoint_is_not_redispatched(tmp_path):
    """A resume_source() dispatch counts as having dispatched the checkpoint
    too, so the next tick does not resume it again (issue #81 re-review, R3)."""
    clock = _Clock(_DAY.replace(hour=8))
    handler = _StickyCheckpointHandler()
    service, db = _service(tmp_path, handler, clock)

    _tick(service)  # today's scheduled run completes
    handler.interrupted_checkpoint = True  # a later scan was interrupted
    clock.now = _DAY.replace(hour=8, minute=1)
    service.trigger_manual("src_daily", trigger="resume")
    service.join_dispatches(timeout=5)
    for minutes in range(2, 5):
        clock.now = _DAY.replace(hour=8) + timedelta(minutes=minutes)
        _tick(service)

    assert [e["trigger"] for e in _executions(db)] == ["scheduled", "resume"]
    assert handler.calls == 2


def test_checkpoint_read_error_at_dispatch_does_not_strand_the_execution(tmp_path):
    """Remembering the dispatched checkpoint happens after the execution is
    reserved: a failing read there must not keep the run from starting, which
    would leave the row 'running' and block the source until a restart."""
    import sqlite3

    class _FlakyCheckpointHandler(_PausableHandler):
        def interrupted_checkpoint_id(self) -> str | None:
            raise sqlite3.OperationalError("database is locked")

    clock = _Clock(_DAY.replace(hour=8))
    handler = _FlakyCheckpointHandler()
    service, db = _service(tmp_path, handler, clock)

    _tick(service)

    assert handler.calls == 1
    assert [(e["status"], e["trigger"]) for e in _executions(db)] == [("completed", "scheduled")]


# -- "Pause scans" for the whole scheduler (the tray menu; update plan F2.2) -------------------------


class _RunningHandler(_PausableHandler):
    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.pause_requests = 0

    def is_running(self) -> bool:
        return self.running

    def request_pause(self) -> bool:
        self.pause_requests += 1
        return True


def test_pause_all_stops_dispatching_and_pauses_running_scans_until_resumed(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _RunningHandler()
    service, db = _service(tmp_path, handler, clock)
    handler.running = True
    assert service.pause_all() == ["src"] and handler.pause_requests == 1
    assert service.paused and service.get_status()["paused"] is True
    handler.running = False
    for minutes in (1, 60, 24 * 60):                     # including the next UTC day
        clock.now = _DAY.replace(hour=8) + timedelta(minutes=minutes)
        _tick(service)
    assert handler.calls == 0 and _executions(db) == []

    handler.paused = True                                # the scan stopped at its checkpoint
    assert service.resume_all() == ["src"]               # resumed through its schedule
    service.join_dispatches(timeout=5)
    assert not service.paused and handler.calls == 1
    assert [e["trigger"] for e in _executions(db)] == ["resume"]


def test_resume_all_leaves_a_source_the_user_had_paused_on_its_own(tmp_path):
    clock = _Clock(_DAY.replace(hour=8))
    handler = _RunningHandler()
    handler.paused = True                                # paused from its card before "Pause scans"
    service, db = _service(tmp_path, handler, clock)
    assert service.pause_all() == []                     # not running: nothing for pause_all to pause
    assert service.resume_all() == []                    # so nothing for resume_all to resume
    service.join_dispatches(timeout=5)
    assert handler.calls == 0 and _executions(db) == []
    _tick(service)
    assert handler.calls == 0                            # still paused, as the user left it


def test_scheduler_pause_routes(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import app

    clock = _Clock(_DAY.replace(hour=8))
    service, _db = _service(tmp_path, _RunningHandler(), clock)
    previous = getattr(app.state, "scheduler", None)
    app.state.scheduler = service
    try:
        client = TestClient(app)
        assert client.post("/api/scheduler/pause").json() == {"paused": True, "pausing": []}
        assert client.get("/api/scheduler/status").json()["paused"] is True
        assert client.post("/api/scheduler/resume").json() == {"paused": False, "resumed": []}
        assert client.get("/api/scheduler/status").json()["paused"] is False
    finally:
        app.state.scheduler = previous
