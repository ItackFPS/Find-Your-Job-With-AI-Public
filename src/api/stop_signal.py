"""Graceful stop without a console (Windows).

The launcher asks its backend child to stop with Ctrl+Break, which only reaches a
process sharing its console. The notification-area app runs under ``pythonw`` and
has no console, so the launcher also sets a named event whose name it passes in
``FYJ_STOP_EVENT``. This thread waits on it and raises SIGBREAK in this process,
which uvicorn handles exactly like Ctrl+Break: running scans stop at their next
checkpoint and the databases close cleanly. Without the variable (a backend not
started by the launcher) nothing is watched.

Same scope as the launcher's own stop event: ``Local\\`` (this logon session), so
only the user's own processes can set it.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

from src.launcher.winproc import IS_WINDOWS, STOP_EVENT_ENV, StopEvent

logger = logging.getLogger(__name__)


def start_stop_signal_watcher() -> StopEvent | None:
    """Watch for the launcher's stop request; returns the event (close it on shutdown)."""
    name = os.environ.get(STOP_EVENT_ENV, "")
    if not IS_WINDOWS or not hasattr(signal, "SIGBREAK") or not name:
        return None
    event = StopEvent(name)
    try:
        if not event.create():
            return None
    except OSError as exc:
        logger.warning("Graceful-stop event unavailable: %s", exc)
        return None

    def watch() -> None:
        while not event.wait(3600.0):
            pass
        logger.info("Stop requested by the launcher; shutting down.")
        signal.raise_signal(signal.SIGBREAK)

    threading.Thread(target=watch, name="launcher-stop-signal", daemon=True).start()
    return event
