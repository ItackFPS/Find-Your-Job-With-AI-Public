"""Tests for the real-time pipeline progress system (SSE).

Covers:
  - ProgressBus: publish/subscribe fan-out, multiple subscribers, best-effort
    behavior when no loop is bound or a subscriber queue is full.
  - PipelineRunner: emits started/progress/completed/failed events that
    mirror the same stats already persisted to RunStorage, and a broken
    progress bus never fails the pipeline itself.
  - GET /api/pipeline/events: snapshot-on-connect, live event streaming,
    subscriber cleanup on disconnect, and that the payload can never carry
    more than RunStats already exposes (no descriptions/secrets/tracebacks).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.api.progress_stream import ProgressBus


# ---------------------------------------------------------------------------
# ProgressBus unit tests
# ---------------------------------------------------------------------------

class TestProgressBus:
    def test_publish_without_bound_loop_is_a_noop(self):
        """Best-effort: publishing before bind_loop() must never raise."""
        bus = ProgressBus()
        bus.publish({"type": "pipeline.started", "run_id": "x"})  # must not raise

    def test_multiple_subscribers_receive_the_same_event(self):
        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            q1 = bus.subscribe()
            q2 = bus.subscribe()

            bus.publish({"type": "pipeline.progress", "run_id": "r1", "stage": "collect"})
            await asyncio.sleep(0.05)  # let call_soon_threadsafe run

            e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
            e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
            assert e1["run_id"] == "r1"
            assert e2["run_id"] == "r1"

        asyncio.run(_body())

    def test_unsubscribe_stops_delivery(self):
        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()
            bus.unsubscribe(q)

            bus.publish({"type": "pipeline.progress", "run_id": "r1"})
            await asyncio.sleep(0.05)
            assert q.empty()

        asyncio.run(_body())

    def test_full_queue_drops_oldest_instead_of_raising(self):
        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()

            for i in range(200):  # far beyond the internal maxsize
                bus.publish({"type": "pipeline.progress", "seq": i})
                await asyncio.sleep(0)
            await asyncio.sleep(0.05)

            # Must not have raised/crashed, and the most recent event must
            # have made it through (oldest dropped, not the publish call).
            last = None
            while not q.empty():
                last = q.get_nowait()
            assert last is not None
            assert last["seq"] == 199

        asyncio.run(_body())


# ---------------------------------------------------------------------------
# PipelineRunner instrumentation
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db_path = str(tmp_path / "test.db")
    s = Storage(db_path=db_path)
    s.init_db()
    s.close()
    rs = RunStorage(db_path=db_path)
    rs.init_db()
    rs.close()
    return db_path


class _RecordingBus:
    """A fake ProgressBus that records publish() calls without needing a loop."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


class _RaisingBus:
    """A fake ProgressBus whose publish() always raises — must never affect
    the pipeline run itself (Phase 3 requirement: best-effort instrumentation)."""

    def publish(self, event: dict) -> None:
        raise RuntimeError("boom: broken progress subsystem")


def _fake_result(stats):
    from src.pipeline.orchestrator import PipelineResult
    return PipelineResult(
        run_id=str(uuid.uuid4()),
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status="completed",
        stats=stats,
        scored_jobs=[],
        rejection_records=[],
    )


class TestPipelineRunnerEvents:
    def test_start_run_publishes_started_event(self, tmp_db):
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineStats

        bus = _RecordingBus()
        runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)

        with patch("src.api.runner.run_pipeline", return_value=_fake_result(PipelineStats())):
            run_id = runner.start_run(use_ai=False)
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)

        types = [e["type"] for e in bus.events]
        assert "pipeline.started" in types
        started = next(e for e in bus.events if e["type"] == "pipeline.started")
        assert started["run_id"] == run_id
        assert started["status"] == "running"

    def test_progress_and_completed_events_mirror_persisted_stats(self, tmp_db):
        """The stats in the SSE event must match what RunStorage persisted —
        no second, divergent representation of pipeline state."""
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineStats
        from src.storage.runs import RunStorage

        bus = _RecordingBus()
        runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)
        stats = PipelineStats(collected=12, validated=10, filtered=6, scored=4, personalized=3)

        def fake_run_pipeline(*args, progress_callback=None, **kwargs):
            if progress_callback is not None:
                progress_callback("collect", stats.to_dict())
                progress_callback("score", stats.to_dict())
            return _fake_result(stats)

        with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
            run_id = runner.start_run(use_ai=False)
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)

        progress_events = [e for e in bus.events if e["type"] == "pipeline.progress"]
        assert [e["stage"] for e in progress_events] == ["collect", "score"]
        for e in progress_events:
            assert e["run_id"] == run_id
            assert e["stats"]["collected"] == 12

        completed = [e for e in bus.events if e["type"] == "pipeline.completed"]
        assert len(completed) == 1
        assert completed[0]["stats"]["personalized"] == 3

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        row = rs.get_run(run_id)
        rs.close()
        assert row["stats"]["personalized"] == completed[0]["stats"]["personalized"]

    def test_failed_run_publishes_failed_event_with_error(self, tmp_db):
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineResult, PipelineStats

        bus = _RecordingBus()
        runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)

        failed_result = PipelineResult(
            run_id=str(uuid.uuid4()),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status="failed",
            stats=PipelineStats(),
            scored_jobs=[],
            rejection_records=[],
            error="boom",
        )
        with patch("src.api.runner.run_pipeline", return_value=failed_result):
            run_id = runner.start_run(use_ai=False)
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)

        failed = [e for e in bus.events if e["type"] == "pipeline.failed"]
        assert len(failed) == 1
        assert failed[0]["run_id"] == run_id
        assert failed[0]["error"] == "boom"

    def test_broken_progress_bus_does_not_fail_the_pipeline(self, tmp_db):
        """A raising publish() must be swallowed — the pipeline run itself
        must still complete successfully (Phase 3 hard requirement)."""
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineStats
        from src.storage.runs import RunStorage

        runner = PipelineRunner(db_path=tmp_db, progress_bus=_RaisingBus())
        stats = PipelineStats(collected=5, scored=2, personalized=1)

        def fake_run_pipeline(*args, progress_callback=None, **kwargs):
            if progress_callback is not None:
                progress_callback("collect", stats.to_dict())
            return _fake_result(stats)

        with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
            run_id = runner.start_run(use_ai=False)
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        row = rs.get_run(run_id)
        rs.close()
        assert row["status"] == "completed"
        assert row["stats"]["personalized"] == 1

    def test_no_progress_bus_is_safe(self, tmp_db):
        """Omitting progress_bus entirely (default None) must not error —
        preserves behavior for any existing caller that doesn't pass one."""
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineStats

        runner = PipelineRunner(db_path=tmp_db)  # no progress_bus kwarg
        with patch("src.api.runner.run_pipeline", return_value=_fake_result(PipelineStats())):
            runner.start_run(use_ai=False)
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)


# ---------------------------------------------------------------------------
# _format_sse / PipelineEventOut whitelist
# ---------------------------------------------------------------------------

class TestEventPayloadSchema:
    def test_unknown_and_sensitive_keys_are_dropped(self):
        from src.api.routes.pipeline import _format_sse

        raw = {
            "type": "pipeline.progress",
            "run_id": "r1",
            "status": "running",
            "stage": "collect",
            "stats": {
                "collected": 5,
                # None of the following are fields on RunStats — must be
                # silently dropped, never reach the wire.
                "webhook_url": "http://localhost:5678/webhook/job-pipeline-SECRET",
                "discord_webhook": "https://discord.com/api/webhooks/SECRET",
                "description": "Full job description text that must not leak",
                "api_key": "sk-should-not-appear",
            },
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        formatted = _format_sse(raw)
        assert "SECRET" not in formatted
        assert "should-not-appear" not in formatted
        assert "Full job description text" not in formatted
        assert '"collected":5' in formatted.replace(" ", "")

    def test_sse_frame_format(self):
        from src.api.routes.pipeline import _format_sse

        formatted = _format_sse({
            "type": "pipeline.started",
            "run_id": "r1",
            "status": "running",
            "stage": "",
            "stats": {},
            "timestamp": "2026-01-01T00:00:00+00:00",
        })
        assert formatted.startswith("event: pipeline.started\n")
        assert "\ndata: " in formatted
        assert formatted.endswith("\n\n")
        payload = json.loads(formatted.split("data: ", 1)[1].strip())
        assert payload["run_id"] == "r1"
        assert payload["status"] == "running"


# ---------------------------------------------------------------------------
# GET /api/pipeline/events — full endpoint behavior
#
# Note on test approach: this project's installed httpx/starlette TestClient
# fully drains an ASGI call before `client.stream(...)` hands back control,
# so it cannot exercise a generator that only ends on client-disconnect (our
# endpoint loops until `request.is_disconnected()`, which itself can't
# become true until the client gets control back — a deadlock that is a
# property of that test harness, not of the endpoint). So these tests call
# the real `stream_pipeline_events` coroutine directly and drive its
# `StreamingResponse.body_iterator` by hand, against a minimal fake Request.
# This still exercises the actual production code (subscribe/unsubscribe,
# snapshot building, event formatting) end-to-end, just without a real
# socket in between.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """Shrink the SSE keep-alive interval so tests don't block for the
    production 15s heartbeat while waiting to notice "nothing arrived"."""
    monkeypatch.setattr("src.api.routes.pipeline._SSE_HEARTBEAT_SECONDS", 0.05)


class _FakeAppState:
    def __init__(self, db_path: str, runner, progress_bus) -> None:
        self.db_path = db_path
        self.runner = runner
        self.progress_bus = progress_bus


class _FakeApp:
    def __init__(self, state: _FakeAppState) -> None:
        self.state = state


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — only what the endpoint reads."""

    def __init__(self, db_path: str, runner, progress_bus) -> None:
        self.app = _FakeApp(_FakeAppState(db_path, runner, progress_bus))

    async def is_disconnected(self) -> bool:
        return False


def _extract_data_payloads(chunk: str) -> list[dict]:
    return [
        json.loads(line[len("data:"):].strip())
        for line in chunk.split("\n")
        if line.startswith("data:")
    ]


async def _open_stream(db_path: str, runner, progress_bus):
    """Call the real endpoint and return its live body_iterator, already
    advanced exactly once — far enough to guarantee bus.subscribe() has run
    (subscribe happens on the generator's first __anext__, not on calling
    stream_pipeline_events() itself), but no further. Returns
    (body_iterator, first_frame_payloads).
    """
    from src.api.routes.pipeline import stream_pipeline_events

    request = _FakeRequest(db_path, runner, progress_bus)
    response = await stream_pipeline_events(request)
    gen = response.body_iterator
    first_chunk = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    return gen, _extract_data_payloads(first_chunk)


async def _collect_until(gen, stop_types: set[str], timeout: float = 5.0) -> list[dict]:
    """Keep reading frames from an already-open body_iterator until one of
    stop_types is seen or the timeout elapses."""
    events: list[dict] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
            payloads = _extract_data_payloads(chunk)
            events.extend(payloads)
            if any(p["type"] in stop_types for p in payloads):
                break
    except (asyncio.TimeoutError, StopAsyncIteration):
        pass
    return events


class TestPipelineEventsEndpoint:
    def test_snapshot_is_idle_when_no_runs_exist(self, tmp_db: str):
        from src.api.progress_stream import ProgressBus
        from src.api.runner import PipelineRunner

        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)

            # No runs yet -> no snapshot event; the first frame must be a
            # quiet keep-alive, never bogus data, and no crash.
            gen, first_payloads = await _open_stream(tmp_db, runner, bus)
            assert first_payloads == []
            await gen.aclose()

        asyncio.run(_body())

    def test_full_lifecycle_stream(self, tmp_db: str):
        from src.api.progress_stream import ProgressBus
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineStats

        stats = PipelineStats(collected=8, validated=7, filtered=4, scored=3, personalized=2)

        def fake_run_pipeline(*args, progress_callback=None, **kwargs):
            if progress_callback is not None:
                progress_callback("collect", stats.to_dict())
                progress_callback("score", stats.to_dict())
            return _fake_result(stats)

        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)

            gen, first_payloads = await _open_stream(tmp_db, runner, bus)
            # Subscribed and synced — now it's safe to trigger the run.
            with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
                t = threading.Thread(target=lambda: runner.start_run(use_ai=False), daemon=True)
                t.start()
                events = first_payloads + await _collect_until(
                    gen, stop_types={"pipeline.completed", "pipeline.failed"}
                )
                t.join(timeout=5.0)
            await gen.aclose()
            return events

        events = asyncio.run(_body())

        types = [e["type"] for e in events]
        assert "pipeline.started" in types
        assert "pipeline.progress" in types
        assert types[-1] == "pipeline.completed"

        # Ordering/consistency: exactly one run_id throughout, status only
        # ever moves forward (running -> ... -> completed), never back.
        run_ids = {e["run_id"] for e in events}
        assert len(run_ids) == 1
        assert events[-1]["status"] == "completed"
        assert events[-1]["stats"]["personalized"] == 2

    def test_subscriber_cleanup_on_disconnect(self, tmp_db: str):
        from src.api.progress_stream import ProgressBus
        from src.api.runner import PipelineRunner

        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)

            assert len(bus._subscribers) == 0
            gen, _ = await _open_stream(tmp_db, runner, bus)
            assert len(bus._subscribers) == 1
            await gen.aclose()
            assert len(bus._subscribers) == 0

        asyncio.run(_body())

    def test_multiple_concurrent_subscribers_both_receive_events(self, tmp_db: str):
        from src.api.progress_stream import ProgressBus
        from src.api.runner import PipelineRunner
        from src.pipeline.orchestrator import PipelineStats

        stats = PipelineStats(collected=1, personalized=1)

        def fake_run_pipeline(*args, progress_callback=None, **kwargs):
            return _fake_result(stats)

        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)

            gen1, first1 = await _open_stream(tmp_db, runner, bus)
            gen2, first2 = await _open_stream(tmp_db, runner, bus)
            assert len(bus._subscribers) == 2

            with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
                t = threading.Thread(target=lambda: runner.start_run(use_ai=False), daemon=True)
                t.start()
                events1, events2 = await asyncio.gather(
                    _collect_until(gen1, stop_types={"pipeline.completed", "pipeline.failed"}),
                    _collect_until(gen2, stop_types={"pipeline.completed", "pipeline.failed"}),
                )
                t.join(timeout=5.0)
            await gen1.aclose()
            await gen2.aclose()
            return first1 + events1, first2 + events2

        events1, events2 = asyncio.run(_body())
        assert any(e["type"] == "pipeline.started" for e in events1)
        assert any(e["type"] == "pipeline.started" for e in events2)

    def test_snapshot_reflects_previously_completed_run(self, tmp_db: str):
        """A newly connected client (e.g. after a refresh) must see the
        latest authoritative run state, not start from zero."""
        from src.api.progress_stream import ProgressBus
        from src.api.runner import PipelineRunner
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        rs.create_run("prior-run")
        rs.complete_run("prior-run", {"collected": 42, "personalized": 7, "current_stage": ""})
        rs.close()

        async def _body():
            bus = ProgressBus()
            bus.bind_loop(asyncio.get_running_loop())
            runner = PipelineRunner(db_path=tmp_db, progress_bus=bus)
            gen, first_payloads = await _open_stream(tmp_db, runner, bus)
            await gen.aclose()
            return first_payloads

        events = asyncio.run(_body())

        assert len(events) == 1
        snap = events[0]
        assert snap["type"] == "pipeline.snapshot"
        assert snap["run_id"] == "prior-run"
        assert snap["status"] == "completed"
        assert snap["stats"]["personalized"] == 7
