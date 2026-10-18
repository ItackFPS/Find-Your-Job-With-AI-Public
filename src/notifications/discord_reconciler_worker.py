"""Automatic Discord location-channel reconciliation — runs entirely inside
the backend process, on its own interval, with NO dependency on Claude Code,
an MCP server, or any LLM (LM Studio/Ollama included). It calls
src.discord_admin directly, in-process — the exact same shared
administration library Claude's MCP tools use for manual/on-demand
reconciliation (src/discord_admin/reconciler.py), so there is exactly one
implementation of "how to reconcile," never two.

Reload/duplicate-start safety
------------------------------
This backend runs under ``uvicorn --reload`` in development
(src/api/app.py's module docstring). Uvicorn's reloader is a SEPARATE
supervisor process that never imports the application itself — on every
file change it terminates the one running worker subprocess and starts a
fresh one, which re-imports src.api.app and re-runs its lifespan from
scratch. So under normal operation there is exactly one process (and thus
one DiscordReconcilerWorker instance) alive at a time; --reload does not,
by itself, cause two to run concurrently.

Two independent layers still guard against duplication, per the approved
design (the file lock must never be the ONLY protection):
1. ``start()`` is idempotent PER INSTANCE (guarded by ``_start_lock``): a
   second call while a thread is already running is a no-op + warning log,
   not a second thread. This is the primary guard, and it's what protects
   against an in-process bug (e.g. lifespan accidentally invoked twice on
   the same app object, as can happen in some test harnesses).
2. ``src.discord_admin.reconciler.reconcile()`` still takes the
   cross-process advisory file lock (src/discord_admin/lock.py) for the
   full duration of each reconciliation pass. This is what protects against
   the genuine cross-PROCESS edge case: an old reloader-managed subprocess
   whose shutdown is slow/hung overlapping briefly with a freshly started
   one. Neither layer alone is trusted as sufficient — see the approved
   plan.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from src.config_files import resolve_config_path
from src.discord_admin.client import DiscordAdminClient
from src.discord_admin.lock import default_lock_path
from src.discord_admin.service import run_reconciliation_pass

logger = logging.getLogger(__name__)

# Configurable without a code change: set DISCORD_RECONCILER_INTERVAL_SECONDS
# in .env. Kept short enough that a profile change is picked up promptly,
# long enough to stay well clear of Discord rate limits for a handful of
# location channels.
DEFAULT_INTERVAL_SECONDS = 300


def _interval_from_env(default: int = DEFAULT_INTERVAL_SECONDS) -> int:
    raw = os.environ.get("DISCORD_RECONCILER_INTERVAL_SECONDS")
    if not raw:
        return default
    try:
        value = int(raw)
        if value < 30:
            logger.warning(
                "DISCORD_RECONCILER_INTERVAL_SECONDS=%s is too low — using 30s floor.", raw
            )
            return 30
        return value
    except ValueError:
        logger.warning(
            "DISCORD_RECONCILER_INTERVAL_SECONDS=%r is not a valid integer — using default %ds.",
            raw,
            default,
        )
        return default


class DiscordReconcilerWorker:
    """Background daemon thread that keeps Discord location channels in
    sync with the profile, with no manual trigger required.

    Every tick is independent and stateless-recomputed (desired state from
    the current profile.yaml, known state from the current
    notifications.yaml) — a failed or skipped tick is always safely retried
    on the next one; nothing here holds state across ticks that matters for
    correctness.
    """

    def __init__(
        self,
        config_dir: str,
        profile_path: str | None = None,
        interval_seconds: int | None = None,
        lock_path: Path | None = None,
        env_path: Path | None = None,
        client_factory: Callable[[], DiscordAdminClient] | None = None,
    ) -> None:
        self._config_dir = config_dir
        # None = resolve config/profile(.local).yaml on every tick, so a
        # personal override created while the backend runs is picked up.
        self._profile_path = profile_path
        self._interval_seconds = interval_seconds if interval_seconds is not None else _interval_from_env()
        self._lock_path = lock_path or default_lock_path()
        self._env_path = env_path
        self._client_factory = client_factory

        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op."""
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning(
                    "DiscordReconcilerWorker.start() called while already running — ignoring."
                )
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="discord-reconciler-worker"
            )
            self._thread.start()
            logger.info(
                "Discord reconciliation worker started (interval: %ds).", self._interval_seconds
            )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # A tick was mid-flight (e.g. a Discord rate-limit backoff)
                # and didn't finish within the join timeout. Harmless: the
                # thread will exit on its own right after that tick
                # completes (it checks _stop_event immediately after). The
                # advisory file lock in src.discord_admin.reconciler is what
                # keeps this safe even if start() is called again before
                # that happens — see this module's docstring.
                logger.warning(
                    "DiscordReconcilerWorker.stop(): worker thread still finishing "
                    "its current tick after %.1fs — it will exit on its own shortly.",
                    timeout,
                )
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- loop -------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a tick failure must never kill the loop
                logger.exception("discord_reconciler_worker: unexpected error during tick")
            if self._stop_event.wait(self._interval_seconds):
                return

    # -- one tick -----------------------------------------------------------

    def tick(self) -> str:
        """Run exactly one reconciliation pass via the shared
        src.discord_admin.service orchestration (the SAME function Claude's
        on-demand MCP tool calls — see src/discord_admin/mcp_server.py).
        Returns a short status string for observability/testing:
        "no_credentials" | "no_changes" | "reconciled" | "locked" | "error"
        """
        profile_path = self._profile_path or str(
            resolve_config_path(self._config_dir, "profile.yaml")
        )
        report = run_reconciliation_pass(
            self._config_dir,
            profile_path,
            lock_path=self._lock_path,
            env_path=self._env_path,
            client_factory=self._client_factory,
        )

        for note in report.plan_invalid:
            logger.warning("discord_reconciler_worker: %s", note)
        if report.status == "locked":
            logger.info(
                "discord_reconciler_worker: reconciliation already in progress "
                "(another tick or an on-demand run) — skipping this tick, will retry next interval."
            )
        elif report.status == "error":
            logger.warning("discord_reconciler_worker: tick failed: %s", report.error)
        for outcome in report.outcomes:
            logger.info(
                "discord_reconciler_worker: %s -> %s%s",
                outcome.slug,
                outcome.outcome,
                f" ({outcome.detail})" if outcome.detail else "",
            )
        return report.status
