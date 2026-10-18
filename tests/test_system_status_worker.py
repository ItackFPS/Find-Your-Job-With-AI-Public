"""Tests for src.notifications.system_status_worker.SystemStatusWorker —
the dedicated heartbeat that re-renders the #system-status Discord message.

Covers:
- start() is idempotent (same in-process guard pattern as
  DiscordReconcilerWorker — deliberately NOT the same worker instance/thread,
  see this module's own docstring for why they're kept separate).
- tick() delegates to NotificationEngine.update_system_status() — no
  duplicated status-computation logic in the worker itself.
- A tick that raises never kills the loop thread or propagates.
- Interval is configurable via SYSTEM_STATUS_INTERVAL_SECONDS.
"""

from __future__ import annotations

from unittest.mock import MagicMock


from src.notifications.system_status_worker import (
    DEFAULT_INTERVAL_SECONDS,
    SystemStatusWorker,
    _interval_from_env,
)


class TestStartIsIdempotent:
    def test_second_start_does_not_spawn_a_new_thread(self):
        engine = MagicMock()
        worker = SystemStatusWorker(notification_engine=engine, interval_seconds=1000)
        try:
            worker.start()
            first_thread = worker._thread
            assert first_thread is not None and first_thread.is_alive()

            worker.start()  # second call — must be a no-op
            assert worker._thread is first_thread
        finally:
            worker.stop()

    def test_stop_then_start_again_creates_a_fresh_thread(self):
        engine = MagicMock()
        worker = SystemStatusWorker(notification_engine=engine, interval_seconds=1000)
        worker.start()
        first_thread = worker._thread
        worker.stop()
        assert not first_thread.is_alive()

        worker.start()
        try:
            assert worker._thread is not first_thread
            assert worker.is_running()
        finally:
            worker.stop()

    def test_not_running_before_start(self):
        engine = MagicMock()
        worker = SystemStatusWorker(notification_engine=engine, interval_seconds=1000)
        assert worker.is_running() is False


class TestTickDelegatesToEngine:
    def test_tick_calls_update_system_status(self):
        engine = MagicMock()
        worker = SystemStatusWorker(notification_engine=engine, interval_seconds=1000)
        worker.tick()
        engine.update_system_status.assert_called_once()

    def test_tick_never_raises_even_if_engine_raises(self):
        """A failing heartbeat must never crash the worker loop or the
        application — this is the same "never raises" contract every other
        notification code path in this system honors."""
        engine = MagicMock()
        engine.update_system_status.side_effect = RuntimeError("boom")
        worker = SystemStatusWorker(notification_engine=engine, interval_seconds=1000)
        worker.tick()  # must not raise

    def test_loop_survives_a_raising_tick(self):
        """The background thread itself must keep running (and keep
        ticking) even after one tick's engine call raises."""
        engine = MagicMock()
        engine.update_system_status.side_effect = [RuntimeError("boom"), None, None]
        worker = SystemStatusWorker(notification_engine=engine, interval_seconds=1)
        try:
            worker.start()
            # Give the loop a couple of interval cycles to run past the
            # first (raising) tick.
            worker._stop_event.wait(2.5)
            assert worker.is_running() is True
            assert engine.update_system_status.call_count >= 2
        finally:
            worker.stop()


class TestIntervalFromEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SYSTEM_STATUS_INTERVAL_SECONDS", raising=False)
        assert _interval_from_env() == DEFAULT_INTERVAL_SECONDS

    def test_default_is_five_minutes(self):
        assert DEFAULT_INTERVAL_SECONDS == 300

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_STATUS_INTERVAL_SECONDS", "120")
        assert _interval_from_env() == 120

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_STATUS_INTERVAL_SECONDS", "not-a-number")
        assert _interval_from_env() == DEFAULT_INTERVAL_SECONDS

    def test_too_low_value_is_floored(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_STATUS_INTERVAL_SECONDS", "5")
        assert _interval_from_env() == 30

    def test_worker_uses_env_interval_when_not_explicitly_given(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_STATUS_INTERVAL_SECONDS", "77")
        engine = MagicMock()
        worker = SystemStatusWorker(notification_engine=engine)
        assert worker._interval_seconds == 77
