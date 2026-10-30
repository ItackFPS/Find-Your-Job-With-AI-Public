"""Issue #76: the main pipeline runs daily in the background (pipeline_daily).

* It runs once per UTC day once the user saved their own profile
  (``profile.local.yaml``); on the example profile nothing starts on its own.
* After days with the app closed, one run catches up (never one per missed day).
* It waits while a scan runs or was dispatched in the same tick (no burst on
  jobs.db), and a completed scan starts it through the schedule, so that run
  counts for the day.
* A pipeline run never triggers another pipeline run.
* PipelineHandler maps the runner's outcome to a DispatchResult.

Temporary databases and config directories only; no data/ is touched.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.scheduler.service import (
    PIPELINE_SOURCE_ID,
    DispatchResult,
    PipelineHandler,
    SchedulerService,
)
from src.scheduler.storage import SchedulerStorage
from src.storage.runs import RunStorage

SHIPPED = Path("config/schedules.yaml")


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Handler:
    """A source handler whose runs complete at once; ``running`` simulates a busy source."""

    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def dispatch(self) -> DispatchResult:
        self.dispatch_calls += 1
        return DispatchResult(run_ref=str(uuid.uuid4()), completed=True)


class _IdleRunner:
    """The pipeline_runner the service consults after a scan (never itself running here)."""

    def __init__(self) -> None:
        self.direct_starts = 0

    def is_running(self) -> bool:
        return False

    def start_run(self) -> str:
        self.direct_starts += 1
        return str(uuid.uuid4())


def _defs(config_dir: Path) -> None:
    (config_dir / "schedules.yaml").write_text(
        """schedules:
  - id: pipeline_daily
    source_id: pipeline
    label: Daily search
    enabled: true
    startup_policy: when_profile_saved
    retry_max: 1
    retry_delay_seconds: 1800
  - id: scan_daily
    source_id: scan
    label: A scan
    enabled: true
    startup_policy: run_once_per_day
    retry_max: 0
    retry_delay_seconds: 300
""",
        encoding="utf-8",
    )


def _service(
    tmp_path: Path,
    handlers: dict,
    *,
    profile_saved: bool = True,
    clock: _Clock | None = None,
    runner: _IdleRunner | None = None,
) -> SchedulerService:
    _defs(tmp_path)
    if profile_saved:
        (tmp_path / "profile.local.yaml").write_text("target_titles: []\n", encoding="utf-8")
    svc = SchedulerService(
        db_path=str(tmp_path / "scheduler.db"), config_dir=str(tmp_path), handlers=handlers,
        pipeline_runner=runner, clock=clock,
    )
    storage = SchedulerStorage(str(tmp_path / "scheduler.db"), clock=clock)
    storage.init_db()
    storage.seed_from_yaml(tmp_path / "schedules.yaml")
    storage.close()
    return svc


def _executions(tmp_path: Path, definition_id: str) -> list[dict]:
    storage = SchedulerStorage(str(tmp_path / "scheduler.db"))
    try:
        return storage.list_executions(definition_id=definition_id, limit=50)
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Policy: once per day, only once the profile is the user's own
# ---------------------------------------------------------------------------

def test_nothing_runs_on_the_example_profile(tmp_path: Path) -> None:
    pipeline = _Handler()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline}, profile_saved=False)
    svc._tick(is_startup=True)
    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 0


def test_runs_once_per_day_once_the_profile_is_saved(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 7, 0, tzinfo=UTC))
    pipeline = _Handler()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline}, clock=clock)

    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    clock.now += timedelta(hours=6)
    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1

    clock.now += timedelta(days=1)          # the next UTC day
    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 2


def test_days_missed_while_closed_give_one_catch_up_run(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 26, 9, 0, tzinfo=UTC))
    pipeline = _Handler()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline}, clock=clock)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1

    # The app stays closed for four days, then starts again.
    clock.now = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    restarted = SchedulerService(
        db_path=str(tmp_path / "scheduler.db"), config_dir=str(tmp_path),
        handlers={PIPELINE_SOURCE_ID: pipeline}, clock=clock,
    )
    restarted._tick(is_startup=True)
    restarted._tick(is_startup=False)
    restarted.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 2


# ---------------------------------------------------------------------------
# No burst: the pipeline waits for scans, and a completed scan starts it once
# ---------------------------------------------------------------------------

def test_waits_while_a_scan_is_running(tmp_path: Path) -> None:
    # A scan started by hand ("Scan now"): its source has no schedule of its own here.
    pipeline, scan = _Handler(), _Handler()
    scan.running = True
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline, "manual_scan": scan})
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 0
    assert _executions(tmp_path, "pipeline_daily") == []

    scan.running = False
    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1


def test_waits_for_a_scan_dispatched_in_the_same_tick(tmp_path: Path) -> None:
    # No pipeline_runner: the completed scan cannot start the pipeline here, so the
    # pipeline's own schedule runs it on the next tick.
    pipeline, scan = _Handler(), _Handler()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline, "scan": scan})
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert (scan.dispatch_calls, pipeline.dispatch_calls) == (1, 0)

    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1


def test_a_completed_scan_starts_the_pipeline_through_its_schedule(tmp_path: Path) -> None:
    pipeline, scan, runner = _Handler(), _Handler(), _IdleRunner()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline, "scan": scan}, runner=runner)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    svc.join_dispatches(timeout=5)          # the run the scan's completion started

    assert scan.dispatch_calls == 1
    assert pipeline.dispatch_calls == 1
    assert runner.direct_starts == 0
    runs = _executions(tmp_path, "pipeline_daily")
    assert [(r["trigger"], r["status"]) for r in runs] == [("after_scan", "completed")]

    svc._tick(is_startup=False)             # it counted for the day: no second run
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1


def test_a_failed_pipeline_start_leaves_the_scan_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, scan, runner = _Handler(), _Handler(), _IdleRunner()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline, "scan": scan}, runner=runner)

    def schedule_gone(definition_id: str, trigger: str = "manual") -> str | None:
        raise ValueError(f"Schedule {definition_id!r} is disabled")

    monkeypatch.setattr(svc, "trigger_manual", schedule_gone)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert [r["status"] for r in _executions(tmp_path, "scan_daily")] == ["completed"]
    assert pipeline.dispatch_calls == 0


def test_without_a_pipeline_schedule_a_scan_still_starts_the_runner(tmp_path: Path) -> None:
    scan, runner = _Handler(), _IdleRunner()
    svc = _service(tmp_path, {"scan": scan}, runner=runner)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert runner.direct_starts == 1


def test_a_pipeline_run_does_not_start_another(tmp_path: Path) -> None:
    pipeline, runner = _Handler(), _IdleRunner()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline}, runner=runner)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1
    assert runner.direct_starts == 0
    assert len(_executions(tmp_path, "pipeline_daily")) == 1


# ---------------------------------------------------------------------------
# A manual run ("Run pipeline") counts as the day's run (owner decision, 2026-10-30)
# ---------------------------------------------------------------------------

class _WaitingHandler(_Handler):
    """A pipeline handler whose adopted runs end with ``outcome``."""

    def __init__(self, completed: bool = True) -> None:
        super().__init__()
        self.completed = completed
        self.waited: list[str] = []

    def wait(self, run_id: str) -> DispatchResult:
        self.waited.append(run_id)
        return DispatchResult(run_ref=run_id, completed=self.completed)


def test_a_completed_manual_run_counts_for_the_day(tmp_path: Path) -> None:
    pipeline = _WaitingHandler()
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline})
    assert svc.adopt_pipeline_run("run-1") is not None
    svc.join_dispatches(timeout=5)
    assert pipeline.waited == ["run-1"]
    runs = _executions(tmp_path, "pipeline_daily")
    assert [(r["trigger"], r["status"], r["run_ref"]) for r in runs] == [("manual", "completed", "run-1")]

    svc._tick(is_startup=True)              # the day's run already happened
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 0


def test_a_failed_manual_run_does_not_count(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 7, 0, tzinfo=UTC))
    pipeline = _WaitingHandler(completed=False)
    svc = _service(tmp_path, {PIPELINE_SOURCE_ID: pipeline}, clock=clock)
    svc.adopt_pipeline_run("run-1")
    svc.join_dispatches(timeout=5)
    clock.now += timedelta(hours=1)         # after the retry delay (30 min)
    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)
    assert pipeline.dispatch_calls == 1


def test_a_manual_run_is_not_recorded_without_a_pipeline_schedule(tmp_path: Path) -> None:
    svc = _service(tmp_path, {"scan": _Handler()})
    assert svc.adopt_pipeline_run("run-1") is None


# ---------------------------------------------------------------------------
# PipelineHandler
# ---------------------------------------------------------------------------

class _Runner:
    """A PipelineRunner stand-in: start_run records a run that finishes with ``outcome``
    once ``release`` is set."""

    def __init__(self, db_path: str, outcome: str = "completed", busy: bool = False,
                 stats: dict | None = None) -> None:
        self._db_path = db_path
        self._outcome = outcome
        self._stats = stats or {}
        self._busy = busy
        self._current: str | None = None
        self.release = threading.Event()
        self.release.set()

    def is_running(self) -> bool:
        return self._busy or self._current is not None

    def current_run_id(self) -> str | None:
        return self._current

    def start_run(self) -> str:
        if self._busy:
            raise RuntimeError("A pipeline run is already in progress")
        run_id = str(uuid.uuid4())
        rs = RunStorage(db_path=self._db_path)
        rs.init_db()
        rs.create_run(run_id)
        rs.close()
        self._current = run_id

        def finish() -> None:
            self.release.wait(5)
            rs2 = RunStorage(db_path=self._db_path)
            if self._outcome == "completed":
                rs2.complete_run(run_id, self._stats)
            else:
                rs2.fail_run(run_id, "boom")
            rs2.close()
            self._current = None

        threading.Thread(target=finish, daemon=True).start()
        return run_id


@pytest.mark.parametrize("outcome,completed", [("completed", True), ("failed", False)])
def test_handler_reports_the_run_outcome(tmp_path: Path, outcome: str, completed: bool) -> None:
    db = str(tmp_path / "jobs.db")
    handler = PipelineHandler(runner=_Runner(db, outcome), db_path=db, poll_seconds=0.01)
    result = handler.dispatch()
    assert result.run_ref is not None
    assert (result.completed, result.interrupted) == (completed, False)


def test_a_run_that_reached_no_source_is_not_the_days_run(tmp_path: Path) -> None:
    """Waking up offline at the daily time: the run completes with every source unreachable
    (the pipeline marks a network outage). It counts as a failed attempt, retried after its
    backoff, not as the day's run."""
    db = str(tmp_path / "jobs.db")
    offline = {"by_source": {"france-travail-web": {"status": "failed", "network_outage": True},
                             "eures": {"status": "failed", "network_outage": True}}}
    result = PipelineHandler(runner=_Runner(db, stats=offline), db_path=db, poll_seconds=0.01).dispatch()
    assert (result.completed, result.interrupted) == (False, False)
    one_down = {"by_source": {"france-travail-web": {"status": "completed"}, "eures": {"status": "failed"}}}
    assert PipelineHandler(runner=_Runner(db, stats=one_down), db_path=db, poll_seconds=0.01).dispatch().completed


def test_handler_reports_interrupted_on_shutdown(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    runner = _Runner(db)
    runner.release.clear()                  # the run never finishes on its own
    shutdown = threading.Event()
    handler = PipelineHandler(runner=runner, db_path=db, shutdown_event=shutdown, poll_seconds=0.01)
    threading.Timer(0.05, shutdown.set).start()
    result = handler.dispatch()
    runner.release.set()
    assert (result.completed, result.interrupted) == (False, True)


def test_handler_skips_when_a_run_is_already_in_progress(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    handler = PipelineHandler(runner=_Runner(db, busy=True), db_path=db, poll_seconds=0.01)
    assert handler.is_running() is True
    assert handler.dispatch() == DispatchResult(run_ref=None, completed=False)


# ---------------------------------------------------------------------------
# The shipped schedule
# ---------------------------------------------------------------------------

def test_shipped_pipeline_daily_runs_once_the_profile_is_saved() -> None:
    import yaml

    entries = (yaml.safe_load(SHIPPED.read_text(encoding="utf-8")) or {})["schedules"]
    entry = next(e for e in entries if e["id"] == "pipeline_daily")
    assert entry["source_id"] == PIPELINE_SOURCE_ID
    assert entry["enabled"] is True
    assert entry["startup_policy"] == "when_profile_saved"
