"""Tests for the pause/resume helpers shared by the scanners (issue #81)."""

from __future__ import annotations

import threading
import time

from src.scan_control import StopSignal, config_fingerprint, scan_state, wait_for_stop


class TestStopSignal:
    def test_reads_as_set_when_any_event_is_set_and_records_it(self) -> None:
        a, b = threading.Event(), threading.Event()
        stop = StopSignal(a, b)
        assert stop.is_set() is False
        assert stop.observed is False
        b.set()
        assert stop.is_set() is True
        assert stop.observed is True


class TestWaitForStop:
    def test_returns_early_when_stop_is_requested(self) -> None:
        pause = threading.Event()
        stop = StopSignal(pause)
        threading.Timer(0.1, pause.set).start()
        started = time.monotonic()
        assert wait_for_stop(stop, 30.0) is True
        assert time.monotonic() - started < 5

    def test_waits_the_full_delay_when_nothing_happens(self) -> None:
        stop = StopSignal(threading.Event())
        started = time.monotonic()
        assert wait_for_stop(stop, 0.3, slice_seconds=0.05) is False
        assert time.monotonic() - started >= 0.29


class TestScanState:
    def test_running_and_pausing(self) -> None:
        assert scan_state(True, False, None) == "running"
        assert scan_state(True, True, {"state": "running"}) == "pausing"

    def test_idle_states(self) -> None:
        assert scan_state(False, False, None) == "idle"
        assert scan_state(False, False, {"state": "failed"}) == "idle"

    def test_paused_and_interrupted(self) -> None:
        assert scan_state(False, False, {"state": "paused"}) == "paused"
        assert scan_state(False, False, {"state": "interrupted"}) == "interrupted"
        # Legacy row (no state) and an orphaned 'running' row both resume.
        assert scan_state(False, False, {"state": None}) == "interrupted"
        assert scan_state(False, False, {"state": "running"}) == "interrupted"


class TestInterruptedCheckpointId:
    def test_only_interrupted_or_legacy_rows_qualify(self) -> None:
        from src.scan_control import interrupted_checkpoint_id

        row = {"scan_id": "s1", "updated_at": "t1"}
        assert interrupted_checkpoint_id(None) is None
        assert interrupted_checkpoint_id({**row, "state": "interrupted"}) == "s1|t1"
        assert interrupted_checkpoint_id({**row, "state": None}) == "s1|t1"
        # A 'running' row left by a live process (e.g. its 'failed' label
        # could not be written) is not proof of an interruption.
        for state in ("running", "paused", "failed"):
            assert interrupted_checkpoint_id({**row, "state": state}) is None

    def test_identity_changes_when_the_checkpoint_is_rewritten(self) -> None:
        from src.scan_control import interrupted_checkpoint_id

        a = interrupted_checkpoint_id({"scan_id": "s1", "updated_at": "t1", "state": "interrupted"})
        b = interrupted_checkpoint_id({"scan_id": "s2", "updated_at": "t2", "state": "interrupted"})
        assert a != b


def test_config_fingerprint_is_stable_and_order_independent() -> None:
    assert config_fingerprint({"a": 1, "b": 2}) == config_fingerprint({"b": 2, "a": 1})
    assert config_fingerprint({"a": 1}) != config_fingerprint({"a": 2})
