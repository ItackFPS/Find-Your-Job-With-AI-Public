"""Generic background scheduler service.

Architecture
------------
SchedulerService
  - Reads schedule_definitions from SQLite (seeded from config/schedules.yaml)
  - On startup: dispatches any definition where startup_policy is satisfied
  - Background thread: re-checks every POLL_INTERVAL seconds (handles day rollover)
  - Dispatches to registered SourceHandlers by source_id
  - Records every execution in schedule_executions (persistent across restarts)

Startup policy: run_once_per_day (or when_opted_in: the same, once the user chose the
source, plan rule I1; or when_profile_saved: the same, once the user saved their own
profile, config/profile.local.yaml; disabled before)
  A run is dispatched when no completed execution exists for today (UTC midnight).
  If the backend starts at 07:00 → scan runs.
  If the backend restarts at 14:00 (after a scan already ran) → no second scan.
  At UTC midnight the "today" window rolls over → scan runs again next tick.

Retries (issue #68) — outcome of each execution, per UTC day:
  completed    → nothing more today.
  failed       → a genuine failure (exception, or the source did not complete
                 for a reason other than shutdown). Counts against retry_max.
  interrupted  → stopped for backend shutdown, or found still 'running' at
                 startup. Resumable (checkpoint), NOT counted; resumed next tick.
  skipped      → the source was already running; nothing was dispatched.
  A source stopped on a 429 (F6.4) records 'interrupted' and is not dispatched at all
  until its cooldown has passed; then the normal policy resumes its checkpoint.
  After F failed attempts today: if F > retry_max → no more automatic
  attempts until UTC midnight (logged once). Otherwise attempt F + 1 runs once
  retry_delay_seconds * 2**(F-1) (capped at MAX_RETRY_BACKOFF_SECONDS) has
  elapsed since the last failure. retry_max: 0 = no automatic retry after a
  failure. A manual trigger always runs (explicit user action).

The pipeline schedule (issue #76)
  The main pipeline is itself a source handler (source_id "pipeline", PipelineHandler):
  pipeline_daily runs it once per UTC day, so the official/public sources are collected
  in the background. A day missed while the app was closed gives one run, not one per
  missed day. Its automatic run waits while a scan runs or was dispatched in the same
  tick (both write jobs.db), and a completed scan triggers the pipeline through this
  schedule, so that run counts for the day.

Concurrency
  - try_reserve_execution uses BEGIN IMMEDIATE (atomic across multiple processes).
  - handler.is_running() provides an in-process early-exit guard.
  - Each dispatch runs in a dedicated daemon thread so the scheduler loop never blocks.

Timezones and the day (issue #80)
  All timestamps are UTC. With a ``background`` settings reader (the app passes
  data/settings.json), a schedule's "day" is the period starting at the latest
  local ``daily_time`` (default 08:00, on the system clock's own DST rules): a run
  is due when none completed since then, so it stays at the same wall-clock time
  across a clock change, and days missed give one run. Retries are counted per
  period. ``enabled: false`` stops every automatic run (manual ones still work).
  Without a reader (tests, older callers) the day is the UTC day.

Adding a new source
  1. Implement a SourceHandler (is_running / dispatch).
  2. Add an entry to config/schedules.yaml.
  3. Register the handler: SchedulerService(handlers={"my_source": MyHandler()}).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from src.app_settings import BACKGROUND_DEFAULTS, parse_daily_time
from src.config_files import local_config_path
from src.scheduler.storage import SchedulerStorage
from src.scheduler.models import ScheduleDefinition

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # seconds between scheduler loop ticks
# Upper bound on the wait between two automatic attempts of the same day.
MAX_RETRY_BACKOFF_SECONDS = 6 * 3600
# The handler key of the main pipeline (PipelineHandler); every other handler is a scan.
PIPELINE_SOURCE_ID = "pipeline"
# How often PipelineHandler checks whether its run has finished.
PIPELINE_POLL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Source handler protocol
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one handler.dispatch() call.

    run_ref: identifier for the underlying run (e.g. scan_id), or None if
        nothing was dispatched (e.g. another run was already in progress).
    completed: True only if the underlying operation genuinely finished --
        False for "already running", a cooperative shutdown stop, or an
        internally-caught failure inside the source's own scan loop. Drives
        whether SchedulerService records this schedule_execution as
        'completed' or 'failed', and whether the pipeline auto-triggers
        afterward. Added after an incident where a scan stopped for backend
        shutdown (status 'failed', checkpoint preserved) was nevertheless
        recorded as a 'completed' schedule_execution, since dispatch()
        merely not raising was previously read as success.
    interrupted: True when the run did not complete because the backend is
        shutting down (cooperative stop, checkpoint kept). Recorded as
        'interrupted', not 'failed': it is resumed on the next tick and does
        not consume the retry budget (issue #68).
    paused: True when the run stopped at a checkpoint because the user paused
        it. Recorded as 'interrupted' (not a failure); the source is then
        skipped by the scheduler until it is resumed (issue #81).
    rate_limited: True when the run stopped on the source's first 429 (F6.4).
        Recorded as 'interrupted' (not a failure); the source is skipped until
        its cooldown has passed, then resumed from its checkpoint.
    """

    run_ref: str | None
    completed: bool
    interrupted: bool = False
    paused: bool = False
    rate_limited: bool = False


def _flag(obj: Any, name: str) -> bool:
    """Call an optional boolean method; anything but a real True is False.

    Pause/resume hooks are optional on handlers and scanners (issue #81), and
    test doubles such as MagicMock must not read as "paused".
    """
    fn = getattr(obj, name, None)
    return callable(fn) and fn() is True


def _marker(obj: Any, name: str) -> str | None:
    """Call an optional method returning a string identity; anything else is None."""
    fn = getattr(obj, name, None)
    value = fn() if callable(fn) else None
    return value if isinstance(value, str) else None


def _scanner_dispatch(scanner: Any) -> DispatchResult:
    """Shared by the scanner-backed handlers below."""
    run_ref = scanner.run_scan()
    completed = run_ref is not None and bool(scanner.last_scan_completed())
    paused = run_ref is not None and not completed and _flag(scanner, "last_scan_paused")
    rate_limited = run_ref is not None and not completed and _flag(scanner, "last_scan_rate_limited")
    interrupted = run_ref is not None and not completed and bool(scanner.shutdown_requested())
    return DispatchResult(run_ref=run_ref, completed=completed, interrupted=interrupted, paused=paused,
                          rate_limited=rate_limited)


@runtime_checkable
class SourceHandler(Protocol):
    """Interface that every schedulable source must implement."""

    def is_running(self) -> bool:
        """Return True if a run is already in progress for this source."""
        ...

    def dispatch(self) -> DispatchResult:
        """Perform one run synchronously. Returns its outcome (run reference + whether it genuinely completed)."""
        ...


# ---------------------------------------------------------------------------
# LinkedIn handler (wraps existing LinkedInScanner)
# ---------------------------------------------------------------------------

class LinkedInHandler:
    """Adapts LinkedInScanner to the SourceHandler interface."""

    def __init__(self, scanner: Any) -> None:
        self._scanner = scanner

    def is_running(self) -> bool:
        running: bool = self._scanner.is_scanning()
        return running

    def is_paused(self) -> bool:
        return _flag(self._scanner, "is_paused")

    def interrupted_checkpoint_id(self) -> str | None:
        return _marker(self._scanner, "interrupted_checkpoint_id")

    def rate_limited_until(self) -> str | None:
        return _marker(self._scanner, "rate_limited_until")

    def request_pause(self) -> bool:
        """Ask a running scan to stop at its next checkpoint (False when none runs)."""
        return bool(self._scanner.request_pause())

    def dispatch(self) -> DispatchResult:
        return _scanner_dispatch(self._scanner)


# ---------------------------------------------------------------------------
# HelloWork handler (wraps HelloWorkScanner)
# ---------------------------------------------------------------------------

class HelloWorkHandler:
    """Adapts HelloWorkScanner to the SourceHandler interface."""

    def __init__(self, scanner: Any) -> None:
        self._scanner = scanner

    def is_running(self) -> bool:
        running: bool = self._scanner.is_scanning()
        return running

    def is_paused(self) -> bool:
        return _flag(self._scanner, "is_paused")

    def interrupted_checkpoint_id(self) -> str | None:
        return _marker(self._scanner, "interrupted_checkpoint_id")

    def rate_limited_until(self) -> str | None:
        return _marker(self._scanner, "rate_limited_until")

    def request_pause(self) -> bool:
        """Ask a running scan to stop at its next checkpoint (False when none runs)."""
        return bool(self._scanner.request_pause())

    def dispatch(self) -> DispatchResult:
        return _scanner_dispatch(self._scanner)


# ---------------------------------------------------------------------------
# Pipeline handler (wraps PipelineRunner, issue #76)
# ---------------------------------------------------------------------------

class PipelineHandler:
    """Adapts PipelineRunner to the SourceHandler interface.

    The runner starts each run in its own thread; dispatch() waits for it to
    finish, then reads its outcome from the pipeline_runs table. A backend
    shutdown while it waits reports the run as interrupted (the run thread is a
    daemon and dies with the process).
    """

    def __init__(
        self,
        runner: Any,
        db_path: str,
        shutdown_event: threading.Event | None = None,
        poll_seconds: float = PIPELINE_POLL_SECONDS,
    ) -> None:
        self._runner = runner
        self._db_path = db_path
        self._shutdown = shutdown_event or threading.Event()
        self._poll_seconds = poll_seconds

    def is_running(self) -> bool:
        return bool(self._runner.is_running())

    def dispatch(self) -> DispatchResult:
        try:
            run_id = self._runner.start_run()
        except RuntimeError:
            # A run started elsewhere ("Run" on the dashboard) holds the slot.
            return DispatchResult(run_ref=None, completed=False)
        return self.wait(run_id)

    def wait(self, run_id: str) -> DispatchResult:
        """Wait for the run ``run_id`` (already started) to finish and report its outcome."""
        while self._runner.current_run_id() == run_id:
            if self._shutdown.wait(self._poll_seconds):
                return DispatchResult(run_ref=run_id, completed=False, interrupted=True)
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=self._db_path)
        try:
            run = rs.get_run(run_id)
        finally:
            rs.close()
        completed = run is not None and run.get("status") == "completed"
        # A run that reached no source (the pipeline marks a network outage, e.g. the
        # machine woke up offline) is not the day's run: a failed attempt, retried after
        # its backoff.
        by_source = ((run or {}).get("stats") or {}).get("by_source") or {}
        if completed and any(isinstance(r, dict) and r.get("network_outage") for r in by_source.values()):
            logger.warning("Scheduler: pipeline run %s reached no source (network down?); "
                           "not counted as the day's run.", run_id)
            completed = False
        return DispatchResult(run_ref=run_id, completed=completed)


# ---------------------------------------------------------------------------
# Scheduler service
# ---------------------------------------------------------------------------

class SchedulerService:
    """Manages scheduled source runs, persisting state in SQLite."""

    def __init__(
        self,
        db_path: str,
        config_dir: str,
        handlers: dict[str, SourceHandler],
        pipeline_runner: Any = None,
        clock: Callable[[], datetime] | None = None,
        opted_in: Callable[[str], bool] | None = None,
        background: Callable[[], Mapping[str, Any]] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._db_path = db_path
        # The background settings (issue #80): ``enabled`` and the local ``daily_time`` that
        # starts each day's period. None: always enabled, UTC days. ``tz`` None: the system's
        # local time.
        self._background = background
        self._tz = tz
        # Whether the user chose a source (plan rule I1, F6.4): a ``when_opted_in`` schedule
        # runs daily only then. None: nothing is chosen.
        self._opted_in: Callable[[str], bool] = opted_in or (lambda _source: False)
        self._config_dir = config_dir
        self._handlers = handlers
        self._pipeline_runner = pipeline_runner
        self._now: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._dispatch_threads: list[threading.Thread] = []
        # (definition id, UTC day) pairs already logged as out of attempts,
        # so the terminal state is reported once, not on every 60 s tick.
        self._terminal_logged: set[tuple[str, str]] = set()
        # definition id -> identity of the interrupted checkpoint already
        # dispatched (by any trigger), so each interruption is resumed at most
        # once rather than on every tick if the run leaves the checkpoint
        # unchanged (issue #81 review, F1; re-review, R3). In memory on purpose: after a restart an
        # interruption gets one fresh attempt, like any startup recovery.
        self._resumed_checkpoints: dict[str, str] = {}
        # "Pause scans" (the tray menu, POST /api/scheduler/pause): no scheduled dispatch while set.
        # In memory on purpose: a restart of the app resumes its schedule (a paused scan itself stays
        # paused across restarts, as before, until it is resumed).
        self._paused = threading.Event()
        # The scans "Pause scans" itself paused: "Resume scans" resumes these only, never a source
        # the user had paused on its own before (that one stays paused until resumed on its card).
        self._paused_by_pause_all: set[str] = set()

    def _open_storage(self) -> SchedulerStorage:
        return SchedulerStorage(self._db_path, clock=self._now)

    def join_dispatches(self, timeout: float | None = None) -> None:
        """Wait for dispatch threads started so far (used by tests and shutdown)."""
        for t in list(self._dispatch_threads):
            t.join(timeout)
        self._dispatch_threads = [t for t in self._dispatch_threads if t.is_alive()]

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Initialize storage, recover stale executions, start the background loop."""
        storage = self._open_storage()
        try:
            storage.init_db()
            schedules_path = Path(self._config_dir) / "schedules.yaml"
            storage.seed_from_yaml(schedules_path)
            recovered = storage.recover_stale_executions()
            if recovered:
                logger.info("Scheduler: recovered %d stale execution(s).", recovered)
        finally:
            storage.close()

        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="scheduler-loop",
        )
        self._thread.start()
        logger.info("Scheduler started (poll interval: %ds).", POLL_INTERVAL)

    def stop(self) -> None:
        """Signal the background thread to stop.  Returns immediately."""
        self._stop_event.set()

    # -- pause / resume (the tray's "Pause scans") ----------------------------

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause_all(self) -> list[str]:
        """Stop dispatching scheduled runs and ask every running scan to pause at its next
        checkpoint. Returns the sources asked to pause."""
        self._paused.set()
        asked = []
        for source_id, handler in self._handlers.items():
            pause = getattr(handler, "request_pause", None)
            if callable(pause) and handler.is_running() and pause():
                asked.append(source_id)
                self._paused_by_pause_all.add(source_id)
        logger.info("Scheduler: paused (scans asked to pause: %s).", ", ".join(asked) or "none")
        return asked

    def resume_all(self) -> list[str]:
        """Dispatch scheduled runs again and resume, through its schedule (from its checkpoint),
        every scan that ``pause_all`` paused. Returns the sources resumed."""
        self._paused.clear()
        ours, self._paused_by_pause_all = self._paused_by_pause_all, set()
        resumed = []
        for source_id, handler in self._handlers.items():
            if source_id not in ours or not _flag(handler, "is_paused"):
                continue
            try:
                if self.resume_source(source_id) is not None:
                    resumed.append(source_id)
            except (RuntimeError, ValueError) as exc:
                logger.info("Scheduler: %s not resumed: %s", source_id, exc)
        logger.info("Scheduler: resumed (sources resumed: %s).", ", ".join(resumed) or "none")
        return resumed

    # -- status (for API) -----------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of all definitions and their last execution."""
        storage = self._open_storage()
        try:
            defs = storage.list_all_definitions()
            now = self._now()
            period = self._period_start(now)
            background_on = self._background_enabled()
            result = []
            for defn in defs:
                handler = self._handlers.get(defn.source_id)
                last = storage.get_last_execution(defn.id)
                # Daily in effect: what the user sees as "runs every day" (issue #82).
                daily = (
                    defn.enabled and handler is not None and background_on
                    and self._policy(defn) == "run_once_per_day"
                )
                done = daily and storage.get_last_completed_execution(defn.id, since=period) is not None
                result.append({
                    "id": defn.id,
                    "source_id": defn.source_id,
                    "label": defn.label,
                    "enabled": defn.enabled,
                    "startup_policy": defn.startup_policy,
                    "daily": daily,
                    # Today's run is still to come ("due") or done; the next one starts the
                    # next period. None when nothing runs on its own.
                    "due": daily and not done,
                    "next_run_at": self._next_period_start(period).isoformat() if done else None,
                    "is_running": handler.is_running() if handler else False,
                    "last_execution": last,
                })
            background = dict(self._background_settings()) if self._background is not None else None
            return {"schedules": result, "paused": self.paused, "background": background}
        finally:
            storage.close()

    def resume_source(self, source_id: str) -> str | None:
        """Resume a (paused or interrupted) source through its schedule.

        Going through the schedule records the resumed scan as an execution,
        so once it completes the day counts as done and no second full scan
        is started from page 0 by the next tick (issue #81). The scanner
        itself resumes from its checkpoint. Returns None when the source has
        no enabled schedule (the caller then starts the scanner directly).
        Raises RuntimeError if the source is already running.
        """
        storage = self._open_storage()
        try:
            defn = next(
                (d for d in storage.get_enabled_definitions() if d.source_id == source_id),
                None,
            )
        finally:
            storage.close()
        if defn is None:
            return None
        return self.trigger_manual(defn.id, trigger="resume")

    def trigger_manual(self, definition_id: str, trigger: str = "manual") -> str | None:
        """Trigger an immediate run for a definition, bypassing the startup policy."""
        storage = self._open_storage()
        try:
            defn = storage.get_definition(definition_id)
            if defn is None:
                raise ValueError(f"No schedule definition with id={definition_id!r}")
            if not defn.enabled:
                raise ValueError(f"Schedule {definition_id!r} is disabled")
            handler = self._handlers.get(defn.source_id)
            if handler is None:
                raise ValueError(f"No handler registered for source_id={defn.source_id!r}")
            if handler.is_running():
                raise RuntimeError(f"Source {defn.source_id!r} is already running")

            # An explicit user action: bypasses the retry budget and backoff,
            # but is numbered after today's failed attempts (issue #68).
            failed_today, _last = storage.get_failed_attempts_since(
                definition_id, self._period_start(self._now())
            )
            exec_id = storage.try_reserve_execution(
                definition_id, trigger=trigger, no_completed_since=None,
                attempt=failed_today + 1,
            )
            if exec_id is None:
                raise RuntimeError("Another execution is already running for this definition")

            self._dispatch(defn, exec_id, handler)
            return exec_id
        finally:
            storage.close()

    # -- loop -----------------------------------------------------------------

    def _loop(self) -> None:
        first_tick = True
        while not self._stop_event.is_set():
            try:
                self._tick(is_startup=first_tick)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Scheduler tick failed: %s", exc)
            first_tick = False
            self._stop_event.wait(timeout=POLL_INTERVAL)

    def _tick(self, is_startup: bool = False) -> None:
        storage = self._open_storage()
        try:
            definitions = storage.get_enabled_definitions()
            # Scans first: the pipeline waits for a scan dispatched in this tick
            # (its handler may not report running yet) or still running (issue #76).
            scans = [d for d in definitions if d.source_id != PIPELINE_SOURCE_ID]
            pipelines = [d for d in definitions if d.source_id == PIPELINE_SOURCE_ID]
            scan_dispatched = False
            for defn in scans:
                scan_dispatched |= self._check_and_dispatch(storage, defn, is_startup=is_startup)
            for defn in pipelines:
                if scan_dispatched or self._scan_running():
                    logger.debug("Scheduler: %s waits for the running scan(s).", defn.id)
                    continue
                self._check_and_dispatch(storage, defn, is_startup=is_startup)
        finally:
            storage.close()

    def _scan_running(self) -> bool:
        return any(
            handler.is_running()
            for source_id, handler in self._handlers.items()
            if source_id != PIPELINE_SOURCE_ID
        )

    def _check_and_dispatch(
        self,
        storage: SchedulerStorage,
        defn: ScheduleDefinition,
        is_startup: bool,
    ) -> bool:
        """Dispatch ``defn`` if it is due. Returns True when a run was dispatched."""
        handler = self._handlers.get(defn.source_id)
        if handler is None:
            logger.debug("Scheduler: no handler for source_id=%r — skipping.", defn.source_id)
            return False

        # A user-paused source stays paused, across restarts too, until it is
        # explicitly resumed (issue #81). Checked first: no execution row, no
        # retry bookkeeping, no log line every tick.
        if _flag(handler, "is_paused") or self._paused.is_set():
            return False
        # Background collection switched off by the user (issue #80): nothing automatic.
        if not self._background_enabled():
            return False
        # A source stopped on a 429 waits out its cooldown (F6.4): no daily run, retry or
        # resume before then. Once it has passed, the run below resumes its checkpoint.
        if _marker(handler, "rate_limited_until") is not None:
            return False

        trigger = "startup" if is_startup else "scheduled"
        today_start = self._period_start(self._now())
        no_completed_since: datetime | None = (
            today_start if self._policy(defn) == "run_once_per_day" else None
        )

        # Evaluate the startup policy and the retry state machine.
        attempt = self._next_attempt(storage, defn)
        resumed_checkpoint: str | None = None
        if attempt is None:
            # Nothing is due under the normal policy (today's run completed,
            # the retry budget is spent, or a backoff is pending), yet a later
            # scan — e.g. a manual one — was cut off by a shutdown/crash and
            # left an interrupted checkpoint: resume it now instead of waiting
            # for the next UTC day. Only a checkpoint explicitly labelled
            # 'interrupted' qualifies (never one an error left 'running'), and
            # each one is resumed once, so neither failures nor a resume that
            # leaves the checkpoint untouched can turn into a per-tick loop.
            if self._policy(defn) != "run_once_per_day":
                return False
            resumed_checkpoint = _marker(handler, "interrupted_checkpoint_id")
            if (
                resumed_checkpoint is None
                or self._resumed_checkpoints.get(defn.id) == resumed_checkpoint
            ):
                return False
            failed_today, _last = storage.get_failed_attempts_since(defn.id, today_start)
            attempt = failed_today + 1
            trigger = "resume"
            no_completed_since = None

        # In-process early-exit: don't even try to reserve if already running.
        if handler.is_running():
            logger.debug("Scheduler: %s already running, skipping tick.", defn.id)
            return False

        exec_id = storage.try_reserve_execution(
            defn.id,
            trigger=trigger,
            no_completed_since=no_completed_since,
            attempt=attempt,
        )
        if exec_id is None:
            # Another process beat us to the reservation, or already completed today.
            logger.debug("Scheduler: %s reservation skipped (already reserved/completed).", defn.id)
            return False

        logger.info(
            "Scheduler: dispatching %s (trigger=%s, attempt=%d, exec_id=%s).",
            defn.id, trigger, attempt, exec_id,
        )
        self._dispatch(defn, exec_id, handler)
        return True

    def adopt_pipeline_run(self, run_id: str) -> str | None:
        """Record a pipeline run started outside the scheduler ("Run pipeline" on the
        dashboard) as a manual execution of the pipeline schedule, so a completed one
        counts as the day's run (owner decision, 2026-10-30): a full search the user just
        ran is not repeated minutes later. Returns the execution id, or None when there is
        no enabled pipeline schedule or one of its executions is already running."""
        handler = self._handlers.get(PIPELINE_SOURCE_ID)
        wait = getattr(handler, "wait", None)
        if handler is None or not callable(wait):
            return None
        storage = self._open_storage()
        try:
            defn = next(
                (d for d in storage.get_enabled_definitions() if d.source_id == PIPELINE_SOURCE_ID),
                None,
            )
            if defn is None:
                return None
            failed_today, _last = storage.get_failed_attempts_since(
                defn.id, self._period_start(self._now())
            )
            exec_id = storage.try_reserve_execution(
                defn.id, trigger="manual", no_completed_since=None, attempt=failed_today + 1,
            )
        finally:
            storage.close()
        if exec_id is None:
            return None
        self._dispatch(defn, exec_id, handler, work=lambda: wait(run_id))
        return exec_id

    def _start_pipeline_after_scan(self) -> str | None:
        """Start the pipeline after a completed scan: through the pipeline schedule when
        one is enabled (the run is recorded and counts for the day, issue #76), else
        directly on the runner. Raises RuntimeError when a run is already in progress."""
        if PIPELINE_SOURCE_ID in self._handlers:
            storage = self._open_storage()
            try:
                defn = next(
                    (d for d in storage.get_enabled_definitions()
                     if d.source_id == PIPELINE_SOURCE_ID),
                    None,
                )
            finally:
                storage.close()
            if defn is not None:
                return self.trigger_manual(defn.id, trigger="after_scan")
        run_id: str = self._pipeline_runner.start_run()
        return run_id

    def _background_settings(self) -> Mapping[str, Any]:
        if self._background is None:
            return {}
        try:
            return self._background()
        except Exception as exc:  # noqa: BLE001 — a settings read must never stop the scheduler
            logger.warning("Scheduler: could not read the background settings: %s", exc)
            return BACKGROUND_DEFAULTS

    def _background_enabled(self) -> bool:
        return self._background_settings().get("enabled", True) is not False

    def _next_period_start(self, period: datetime) -> datetime:
        """Start of the period after the one starting at ``period``: a day lasts 23 to 25 h,
        so 26 h later always falls in the next one."""
        return self._period_start(period + timedelta(hours=26))

    def _period_start(self, now: datetime) -> datetime:
        """Start (UTC) of the period containing ``now``: the latest local ``daily_time``, or
        UTC midnight without background settings (issue #80)."""
        if self._background is None:
            return _utc_day_start(now)
        parsed = parse_daily_time(self._background_settings().get("daily_time"))
        hour, minute = parsed or parse_daily_time(BACKGROUND_DEFAULTS["daily_time"]) or (8, 0)
        return period_start(now, time(hour, minute), self._tz)

    def _policy(self, defn: ScheduleDefinition) -> str:
        """The startup policy in effect: ``when_opted_in`` (an unofficial source, plan rule
        I1) is ``run_once_per_day`` once the user chose the source, ``disabled`` before;
        ``when_profile_saved`` (the pipeline, issue #76) is ``run_once_per_day`` once the
        user saved their own profile, so a first start on the example profile sends no
        query the user never chose."""
        if defn.startup_policy == "when_profile_saved":
            saved = local_config_path(self._config_dir, "profile.yaml").is_file()
            return "run_once_per_day" if saved else "disabled"
        if defn.startup_policy != "when_opted_in":
            return defn.startup_policy
        try:
            chosen = bool(self._opted_in(defn.source_id))
        except Exception as exc:  # noqa: BLE001 — a settings read must never stop the scheduler
            logger.warning("Scheduler: could not read the choice for %s: %s", defn.source_id, exc)
            chosen = False
        return "run_once_per_day" if chosen else "disabled"

    def _next_attempt(self, storage: SchedulerStorage, defn: ScheduleDefinition) -> int | None:
        """The attempt number to dispatch now, or None if nothing is due.

        run_once_per_day, per UTC day (issue #68):
          * completed today                      → None (done for the day)
          * no failed attempt today              → 1 (interrupted/skipped
                                                   executions are not failures)
          * failed_today > retry_max             → None (terminal until the
                                                   next UTC day; logged once)
          * last failure + backoff not yet reached → None
          * otherwise                            → failed_today + 1
        """
        if self._policy(defn) != "run_once_per_day":
            return None  # unknown / disabled policy, or a source the user has not chosen
        now = self._now()
        today_start = self._period_start(now)
        if storage.get_last_completed_execution(defn.id, since=today_start) is not None:
            return None
        failed_today, last_failed_at = storage.get_failed_attempts_since(defn.id, today_start)
        if failed_today == 0:
            return 1
        if failed_today > defn.retry_max:
            key = (defn.id, today_start.date().isoformat())
            if key not in self._terminal_logged:
                self._terminal_logged.add(key)
                logger.warning(
                    "Scheduler: %s failed %d time(s) today (retry_max=%d) — no more "
                    "automatic attempts today; next attempt after UTC midnight or on "
                    "a manual trigger.",
                    defn.id, failed_today, defn.retry_max,
                )
            return None
        if last_failed_at is not None:
            wait = retry_backoff_seconds(defn.retry_delay_seconds, failed_today)
            if now < last_failed_at + timedelta(seconds=wait):
                return None
        return failed_today + 1

    def _dispatch(
        self,
        defn: ScheduleDefinition,
        exec_id: str,
        handler: SourceHandler,
        work: Callable[[], DispatchResult] | None = None,
    ) -> None:
        """Launch a daemon thread that runs handler.dispatch() and records the result.

        ``work`` replaces handler.dispatch() when the run was already started
        elsewhere (adopt_pipeline_run waits for it instead of starting one).

        When pipeline_runner is configured, a genuinely completed dispatch
        (result.completed and run_ref is not None) automatically triggers a
        pipeline run.  This is generic — it applies to every registered
        source handler, not just LinkedIn.
        """
        # Any dispatch — scheduled, manual or resume — picks up an interrupted
        # checkpoint if one is present, so remember it as already dispatched:
        # if the run leaves it unchanged, the next tick must not resume it
        # again (issue #81 re-review, R3). Read before the thread starts,
        # while the scanner is still idle. Best effort: the execution is
        # already reserved, so a failed read must not keep the run from
        # starting (at worst the checkpoint is resumed once more).
        try:
            checkpoint = _marker(handler, "interrupted_checkpoint_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scheduler: could not read %s checkpoint state: %s", defn.id, exc)
            checkpoint = None
        if checkpoint is not None:
            self._resumed_checkpoints[defn.id] = checkpoint

        def run() -> None:
            started_at = datetime.now(UTC)
            storage = self._open_storage()
            try:
                result = work() if work is not None else handler.dispatch()
                run_ref = result.run_ref
                duration = (datetime.now(UTC) - started_at).total_seconds()
                if result.completed:
                    storage.complete_execution(exec_id, run_ref=run_ref, duration=duration)
                    logger.info(
                        "Scheduler: %s completed in %.1fs (run_ref=%s).",
                        defn.id, duration, run_ref,
                    )
                elif run_ref is None:
                    # Nothing was run: the source was already busy (a race
                    # with a manual scan). Not a failure (issue #68).
                    storage.skip_execution(
                        exec_id, reason=f"{defn.source_id} was already running; nothing dispatched.",
                        duration=duration,
                    )
                    logger.info("Scheduler: %s skipped — source already running.", defn.id)
                elif result.rate_limited:
                    # Stopped on the source's 429, checkpoint kept: resumed once its
                    # cooldown has passed, not counted against retry_max (F6.4).
                    storage.interrupt_execution(
                        exec_id,
                        error=f"{defn.source_id} rate limited (HTTP 429); resumes after its cooldown "
                              f"(run_ref={run_ref!r}).",
                        duration=duration,
                        run_ref=run_ref,
                    )
                    logger.warning("Scheduler: %s rate limited after %.1fs (run_ref=%s); will resume "
                                   "after its cooldown.", defn.id, duration, run_ref)
                elif result.paused:
                    # Stopped at a checkpoint by the user: not a failure, and
                    # not retried — the source is skipped until resumed.
                    storage.interrupt_execution(
                        exec_id,
                        error=f"{defn.source_id} paused by user (run_ref={run_ref!r}).",
                        duration=duration,
                        run_ref=run_ref,
                    )
                    logger.info(
                        "Scheduler: %s paused by user after %.1fs (run_ref=%s).",
                        defn.id, duration, run_ref,
                    )
                elif result.interrupted or self._stop_event.is_set():
                    # Stopped for backend shutdown, checkpoint kept: resumed on
                    # the next tick/startup, not counted against retry_max.
                    storage.interrupt_execution(
                        exec_id,
                        error=f"{defn.source_id} stopped for backend shutdown (run_ref={run_ref!r}).",
                        duration=duration,
                        run_ref=run_ref,
                    )
                    logger.info(
                        "Scheduler: %s interrupted by shutdown after %.1fs (run_ref=%s); "
                        "will resume.", defn.id, duration, run_ref,
                    )
                else:
                    # A genuine failure inside the source (see
                    # DispatchResult.completed): counts against retry_max.
                    storage.fail_execution(
                        exec_id,
                        error=(
                            f"{defn.source_id} did not complete (run_ref={run_ref!r}); "
                            "see the source's own scan status/checkpoint for details."
                        ),
                        duration=duration,
                        run_ref=run_ref,
                    )
                    logger.warning(
                        "Scheduler: %s did not complete in %.1fs (run_ref=%s) -- "
                        "recorded as failed (retry_max=%d).",
                        defn.id, duration, run_ref, defn.retry_max,
                    )

                # Auto-trigger the pipeline after any successful scan so jobs flow
                # through the full pipeline without manual intervention.  Only once
                # every registered scan handler is idle — the pipeline does its own
                # heavy writes to the same jobs.db file, and starting it while a
                # sibling scan (e.g. LinkedIn) is still running caused sustained
                # "database is locked" contention for the rest of that scan.  When
                # a sibling is still busy, skip here: that sibling's own completion
                # will re-run this same check and trigger the pipeline then.
                other_handlers_busy = any(
                    other.is_running() for other in self._handlers.values() if other is not handler
                )
                if defn.source_id == PIPELINE_SOURCE_ID:
                    pass  # the pipeline itself never triggers another pipeline run
                elif result.completed and run_ref is not None and self._pipeline_runner is not None and not other_handlers_busy:
                    if not self._pipeline_runner.is_running():
                        try:
                            pipeline_run_id = self._start_pipeline_after_scan()
                            logger.info(
                                "Scheduler: auto-triggered pipeline run %s after %s scan %s.",
                                pipeline_run_id, defn.source_id, run_ref,
                            )
                        except (RuntimeError, ValueError) as exc:
                            # ValueError: the pipeline schedule changed meanwhile. Never let
                            # it reach the handler below, which would mark this scan failed.
                            logger.debug(
                                "Scheduler: could not auto-trigger pipeline after %s scan %s: %s",
                                defn.source_id, run_ref, exc,
                            )
                    else:
                        logger.debug(
                            "Scheduler: pipeline already running; skipping auto-trigger "
                            "after %s scan %s.", defn.source_id, run_ref,
                        )
                elif other_handlers_busy:
                    logger.debug(
                        "Scheduler: another scan still running; skipping pipeline "
                        "auto-trigger after %s scan %s.", defn.source_id, run_ref,
                    )

            except Exception as exc:  # noqa: BLE001
                duration = (datetime.now(UTC) - started_at).total_seconds()
                logger.exception("Scheduler: %s failed after %.1fs: %s", defn.id, duration, exc)
                try:
                    if self._stop_event.is_set():
                        storage.interrupt_execution(exec_id, error=str(exc), duration=duration)
                    else:
                        storage.fail_execution(exec_id, error=str(exc), duration=duration)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                storage.close()

        t = threading.Thread(
            target=run,
            daemon=True,
            name=f"scheduler-dispatch-{defn.id}",
        )
        self._dispatch_threads = [d for d in self._dispatch_threads if d.is_alive()] + [t]
        t.start()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_day_start(now: datetime) -> datetime:
    """UTC midnight (00:00:00) of the day containing *now*."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def period_start(now: datetime, daily_time: time, tz: tzinfo | None = None) -> datetime:
    """The latest moment at or before ``now`` whose local wall-clock time is ``daily_time``,
    in UTC. ``tz`` None uses the system's local time (its own DST rules), so the time of day
    stays the same across a clock change."""

    def at(day: Any) -> datetime:
        naive = datetime.combine(day, daily_time)
        return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()

    local_now = now.astimezone(tz) if tz is not None else now.astimezone()
    start = at(local_now.date())
    if start > now:
        start = at(local_now.date() - timedelta(days=1))
    return start.astimezone(UTC)


def retry_backoff_seconds(retry_delay_seconds: int, failed_attempts: int) -> float:
    """Wait after the Nth failed attempt of the day before retrying:
    retry_delay_seconds * 2**(N-1), capped at MAX_RETRY_BACKOFF_SECONDS."""
    exponent = max(0, failed_attempts - 1)
    return float(min(MAX_RETRY_BACKOFF_SECONDS, max(0, retry_delay_seconds) * 2 ** min(exponent, 30)))
