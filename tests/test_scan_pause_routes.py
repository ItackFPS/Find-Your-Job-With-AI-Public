"""API tests for scan pause / resume (issue #81).

POST /api/hellowork/pause and /resume, plus the additive
``state`` / ``progress`` fields of GET /status. Real scanners on isolated
temporary databases; the network side (collect / scrape_jobs) is faked.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# The HelloWork scan imports src.pipeline lazily. Import it here, on the main
# thread, as app startup does: a first import from the scan thread loads
# sentence-transformers/sklearn there and can fail with "can't register
# atexit after shutdown" when this file runs on its own.
import src.pipeline.normalize  # noqa: F401
from src.api.routes import hellowork
from src.collectors.base import CollectorResult


class _Scheduler:
    """Stands in for SchedulerService.resume_source."""

    def __init__(self, exec_id: str | None = "exec-1", error: Exception | None = None) -> None:
        self.exec_id = exec_id
        self.error = error
        self.calls: list[str] = []

    def resume_source(self, source_id: str) -> str | None:
        self.calls.append(source_id)
        if self.error is not None:
            raise self.error
        return self.exec_id


# ---------------------------------------------------------------------------
# HelloWork — full pause → paused status → resume round trip
# ---------------------------------------------------------------------------

@pytest.fixture
def hw_staging(tmp_path: Path):
    from src.hellowork.staging import HelloWorkStagingStorage

    store = HelloWorkStagingStorage(db_path=str(tmp_path / "hw.db"))
    store.init_db()
    yield store
    store.close()


def _hw_client(scanner: Any, staging: Any, scheduler: Any = None) -> TestClient:
    app = FastAPI()
    app.include_router(hellowork.router)
    app.state.hellowork_scanner = scanner
    app.state.hellowork_staging = staging
    app.state.scheduler = scheduler
    return TestClient(app)


def _blocking_collect(release: threading.Event, started: threading.Event):
    """Finishes slug "a", then blocks until released, then checks the stop
    signal before slug "b" exactly like the real collector."""
    def _collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
        skip = skip_slugs or set()
        for slug in ("a", "b"):
            if slug in skip:
                continue
            if slug == "b":
                started.set()
                release.wait(timeout=5)
            if shutdown_event is not None and shutdown_event.is_set():
                break
            on_slug_done(slug, [])
        return CollectorResult(source_name="hellowork", jobs=[], errors=[], duration_seconds=0.1)
    return _collect


def test_hellowork_pause_status_and_resume_round_trip(hw_staging) -> None:
    from src.hellowork.scanner import HelloWorkScanner

    scanner = HelloWorkScanner(config={}, staging=hw_staging)
    client = _hw_client(scanner, hw_staging)
    release, started = threading.Event(), threading.Event()

    with patch(
        "src.collectors.hellowork.HelloWorkCollector.collect",
        side_effect=_blocking_collect(release, started),
    ):
        t = threading.Thread(target=scanner.run_scan)
        t.start()
        assert started.wait(timeout=5)
        assert client.get("/api/hellowork/status").json()["state"] == "running"

        resp = client.post("/api/hellowork/pause")
        assert resp.status_code == 202
        assert resp.json()["state"] == "pausing"
        assert client.post("/api/hellowork/pause").json()["state"] == "pausing"  # idempotent

        # Resuming while the pause is still being honoured is refused.
        assert client.post("/api/hellowork/resume").status_code == 409

        release.set()
        t.join(timeout=5)

    body = client.get("/api/hellowork/status").json()
    assert body["state"] == "paused"
    assert body["is_scanning"] is False
    assert body["progress"]["slugs_completed"] == 1
    assert body["last_scan"]["status"] == "paused"
    # Pausing an already-paused scan is a no-op that reports the same state.
    assert client.post("/api/hellowork/pause").json()["state"] == "paused"

    # Resume without a schedule → the scanner is started directly.
    seen: list[str] = []

    def resumed_collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
        for slug in ("a", "b"):
            if slug not in (skip_slugs or set()):
                seen.append(slug)
                on_slug_done(slug, [])
        return CollectorResult(source_name="hellowork", jobs=[], errors=[], duration_seconds=0.1)

    with patch("src.collectors.hellowork.HelloWorkCollector.collect", side_effect=resumed_collect):
        resp = client.post("/api/hellowork/resume")
        assert resp.status_code == 202
        assert resp.json()["scan_id"] is not None
        assert resp.json()["exec_id"] is None
        for _ in range(100):
            if not scanner.is_scanning():
                break
            threading.Event().wait(0.05)

    assert seen == ["b"]  # slug "a" was not replayed
    final = client.get("/api/hellowork/status").json()
    assert final["state"] == "idle"
    assert final["progress"] is None
    assert final["last_scan"]["status"] == "completed"


def test_hellowork_pause_when_idle_is_a_noop(hw_staging) -> None:
    from src.hellowork.scanner import HelloWorkScanner

    client = _hw_client(HelloWorkScanner(config={}, staging=hw_staging), hw_staging)
    resp = client.post("/api/hellowork/pause")
    assert resp.status_code == 202
    assert resp.json()["state"] == "idle"


def test_hellowork_resume_goes_through_the_schedule(hw_staging) -> None:
    from src.hellowork.scanner import HelloWorkScanner

    scheduler = _Scheduler(exec_id="exec-42")
    client = _hw_client(HelloWorkScanner(config={}, staging=hw_staging), hw_staging, scheduler)
    resp = client.post("/api/hellowork/resume")
    assert resp.status_code == 202
    assert resp.json() == {"scan_id": None, "exec_id": "exec-42"}
    assert scheduler.calls == ["hellowork"]


def test_hellowork_resume_while_shutting_down_is_refused(hw_staging) -> None:
    from src.hellowork.scanner import HelloWorkScanner

    event = threading.Event()
    event.set()
    scanner = HelloWorkScanner(config={}, staging=hw_staging, shutdown_event=event)
    resp = _hw_client(scanner, hw_staging).post("/api/hellowork/resume")
    assert resp.status_code == 409
    assert "shutting down" in resp.json()["detail"]


def test_hellowork_resume_without_a_scheduler_starts_the_scanner_directly(hw_staging) -> None:
    from src.hellowork.scanner import HelloWorkScanner

    scanner = HelloWorkScanner(config={}, staging=hw_staging)
    with patch.object(scanner, "start_scan", return_value="hw-direct") as start:
        resp = _hw_client(scanner, hw_staging, scheduler=None).post("/api/hellowork/resume")
    assert resp.status_code == 202
    assert resp.json() == {"scan_id": "hw-direct", "exec_id": None}
    start.assert_called_once()


def test_hellowork_scheduler_conflict_maps_to_409(hw_staging) -> None:
    from src.hellowork.scanner import HelloWorkScanner

    scheduler = _Scheduler(error=RuntimeError("Source 'hellowork' is already running"))
    client = _hw_client(HelloWorkScanner(config={}, staging=hw_staging), hw_staging, scheduler)
    assert client.post("/api/hellowork/resume").status_code == 409


@pytest.mark.parametrize("path", ["/api/hellowork/pause", "/api/hellowork/resume"])
def test_hellowork_controls_503_when_not_configured(path) -> None:
    assert _hw_client(None, None).post(path).status_code == 503
