"""Tests for src/api/routes/hellowork.py.

Uses an isolated FastAPI app with the hellowork router mounted directly and
app.state set manually — no lifespan, no scheduler, no real database, no
network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import hellowork
from src.hellowork.staging import HelloWorkStagingStorage, job_to_staging_record
from src.pipeline.normalize import normalize_job


class FakeScanner:
    def __init__(self, scanning: bool = False, start_scan_result: str | None = "scan-new", start_scan_error: str | None = None):
        self._scanning = scanning
        self._start_scan_result = start_scan_result
        self._start_scan_error = start_scan_error

    def is_scanning(self) -> bool:
        return self._scanning

    def start_scan(self) -> str:
        if self._start_scan_error:
            raise RuntimeError(self._start_scan_error)
        return self._start_scan_result


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def staging(tmp_db: str):
    store = HelloWorkStagingStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


def _make_app(scanner, staging_store) -> TestClient:
    app = FastAPI()
    app.include_router(hellowork.router)
    app.state.hellowork_scanner = scanner
    app.state.hellowork_staging = staging_store
    return TestClient(app)


def _insert_sample_job(staging: HelloWorkStagingStorage, source_job_id: str = "1") -> None:
    raw = {
        "title": "Développeur Python",
        "company": "Acme",
        "description": "Build things",
        "location": "Paris, France",
        "countries": ["FR"],
        "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{source_job_id}.html",
        "source_job_id": source_job_id,
        "published_at": "2026-09-01T00:00:00Z",
        "type": "full-time",
        "remote": False,
        "work_mode": None,
        "skills": [],
    }
    job = normalize_job("hellowork", raw)
    staging.insert_job(job_to_staging_record(job, "scan-1"))


class TestStatus:
    def test_disabled_when_scanner_none(self) -> None:
        client = _make_app(None, None)
        resp = client.get("/api/hellowork/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "enabled": False, "waiting": 0, "last_scan": None, "is_scanning": False,
            "state": "idle", "progress": None, "resume_after": None,
        }

    def test_enabled_with_waiting_count(self, staging) -> None:
        _insert_sample_job(staging, "1")
        _insert_sample_job(staging, "2")
        client = _make_app(FakeScanner(), staging)
        body = client.get("/api/hellowork/status").json()
        assert body["enabled"] is True
        assert body["waiting"] == 2
        assert body["last_scan"] is None

    def test_is_scanning_reflected(self, staging) -> None:
        client = _make_app(FakeScanner(scanning=True), staging)
        body = client.get("/api/hellowork/status").json()
        assert body["is_scanning"] is True

    def test_last_scan_with_stats(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.complete_scan_run(
            "scan-1", jobs_found=5, jobs_new=3,
            stats_json='{"collector_errors": 0, "collection_duration_seconds": 100.0, "metier_slugs_queried": 10}',
        )
        client = _make_app(FakeScanner(), staging)
        body = client.get("/api/hellowork/status").json()
        assert body["last_scan"]["status"] == "completed"
        assert body["last_scan"]["jobs_found"] == 5
        assert body["last_scan"]["stats"]["metier_slugs_queried"] == 10


class TestTriggerScan:
    def test_returns_scan_id(self, staging) -> None:
        client = _make_app(FakeScanner(start_scan_result="scan-abc"), staging)
        resp = client.post("/api/hellowork/scan")
        assert resp.status_code == 202
        assert resp.json() == {"scan_id": "scan-abc"}

    def test_503_when_scanner_not_configured(self) -> None:
        client = _make_app(None, None)
        resp = client.post("/api/hellowork/scan")
        assert resp.status_code == 503

    def test_409_when_already_scanning(self, staging) -> None:
        client = _make_app(FakeScanner(start_scan_error="A HelloWork scan is already in progress"), staging)
        resp = client.post("/api/hellowork/scan")
        assert resp.status_code == 409


class TestListScans:
    def test_empty_when_no_staging(self) -> None:
        client = _make_app(FakeScanner(), None)
        resp = client.get("/api/hellowork/scans")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_scan_history(self, staging) -> None:
        staging.create_scan_run("scan-1")
        staging.complete_scan_run("scan-1", jobs_found=1, jobs_new=1)
        staging.create_scan_run("scan-2")
        staging.fail_scan_run("scan-2", "boom")

        client = _make_app(FakeScanner(), staging)
        resp = client.get("/api/hellowork/scans")
        body = resp.json()
        assert len(body) == 2
        statuses = {row["id"]: row["status"] for row in body}
        assert statuses["scan-1"] == "completed"
        assert statuses["scan-2"] == "failed"

    def test_respects_limit(self, staging) -> None:
        for i in range(5):
            staging.create_scan_run(f"scan-{i}")
        client = _make_app(FakeScanner(), staging)
        resp = client.get("/api/hellowork/scans?limit=2")
        assert len(resp.json()) == 2
