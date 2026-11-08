"""Source handlers: what the scheduler dispatches, one adapter per source.

``SchedulerService`` (service.py) talks to every source through the ``SourceHandler`` protocol
(``is_running`` / ``dispatch``) and reads the outcome as a ``DispatchResult``. The handlers here
wrap the LinkedIn and HelloWork scanners and the main pipeline (``PipelineRunner``, issue #76).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

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
