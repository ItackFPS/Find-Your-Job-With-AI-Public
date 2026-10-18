"""Background heartbeat that keeps the #system-status Discord message
current — deliberately a SEPARATE worker from
src.notifications.discord_reconciler_worker.DiscordReconcilerWorker, not a
tick added onto it: reconciliation manages Discord channel/webhook
INFRASTRUCTURE (create/adopt/deactivate channels), while this worker reports
application HEALTH (is the backend/database/pipeline/n8n/Discord config
usable right now). The two are independent concerns with independent
failure/interval semantics; coupling them would make either concern's
failures affect the other's schedule for no benefit — see NotificationEngine
.update_system_status()'s docstring for what one heartbeat does.

Same lifecycle shape as DiscordReconcilerWorker on purpose (a proven pattern
in this codebase, not reinvented): idempotent start()/stop(), an interval
read from the environment with a floor, a daemon thread that ticks in a
loop and swallows any exception from a single tick so the loop itself never
dies.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.notifications.engine import NotificationEngine

logger = logging.getLogger(__name__)

# Configurable without a code change: set SYSTEM_STATUS_INTERVAL_SECONDS in
# .env. 5 minutes by default — frequent enough that "last checked" stays
# meaningfully fresh, infrequent enough to stay well clear of Discord rate
# limits and to avoid needless DB/n8n-reachability checks.
DEFAULT_INTERVAL_SECONDS = 300
_MIN_INTERVAL_SECONDS = 30


def _interval_from_env(default: int = DEFAULT_INTERVAL_SECONDS) -> int:
    raw = os.environ.get("SYSTEM_STATUS_INTERVAL_SECONDS")
    if not raw:
        return default
    try:
        value = int(raw)
        if value < _MIN_INTERVAL_SECONDS:
            logger.warning(
                "SYSTEM_STATUS_INTERVAL_SECONDS=%s is too low — using %ds floor.",
                raw, _MIN_INTERVAL_SECONDS,
            )
            return _MIN_INTERVAL_SECONDS
        return value
    except ValueError:
        logger.warning(
            "SYSTEM_STATUS_INTERVAL_SECONDS=%r is not a valid integer — using default %ds.",
            raw, default,
        )
        return default


class SystemStatusWorker:
    """Background daemon thread that re-renders the #system-status standing
    Discord message on a fixed interval, independent of pipeline runs.

    Every tick is stateless-recomputed (fresh DB/n8n/Discord-config checks
    each time) — a failed or skipped tick is always safely retried on the
    next one; nothing here holds state across ticks that matters for
    correctness. NotificationEngine.update_system_status() already edits
    ONE standing message in place (send_or_update), so a heartbeat never
    creates a new Discord message — it only ever refreshes the same one.
    """

    def __init__(
        self,
        notification_engine: "NotificationEngine",
        interval_seconds: int | None = None,
    ) -> None:
        self._engine = notification_engine
        self._interval_seconds = interval_seconds if interval_seconds is not None else _interval_from_env()

        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op."""
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning(
                    "SystemStatusWorker.start() called while already running — ignoring."
                )
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="system-status-worker"
            )
            self._thread.start()
            logger.info(
                "System status worker started (interval: %ds).", self._interval_seconds
            )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "SystemStatusWorker.stop(): worker thread still finishing its "
                    "current tick after %.1fs — it will exit on its own shortly.",
                    timeout,
                )
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- loop -------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            self.tick()
            if self._stop_event.wait(self._interval_seconds):
                return

    # -- one tick -----------------------------------------------------------

    def tick(self) -> None:
        """Run exactly one heartbeat. NEVER raises — a failure here must
        never crash this worker's loop or the application; it's a health
        check about the rest of the system, not a critical path itself.
        """
        try:
            self._engine.update_system_status()
        except Exception:  # noqa: BLE001 - a tick failure must never kill the loop or the app
            logger.exception("system_status_worker: unexpected error during tick")
