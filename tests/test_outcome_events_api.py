"""I18 — outcome event API tests.

Covers: POST /api/jobs/{id}/events (valid/invalid event types, missing job),
GET /api/jobs/{id}/events retrieval, and the PATCH /application-status
handler's new (idempotent) side-effect of recording an
application_status_changed event without altering its existing response
contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models.job import Job
from src.storage.sqlite import Storage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def client(tmp_db: str):
    from src.api.app import app

    app.state.db_path = tmp_db
    return TestClient(app)


@pytest.fixture
def seeded_job(tmp_db: str) -> Job:
    s = Storage(db_path=tmp_db)
    s.init_db()
    job = Job(
        source="test_source",
        source_job_id="JOB-001",
        title="Software Engineer",
        company="Acme Corp",
        location="Paris, France",
        content_hash="abc123",
    )
    s.upsert_job(job)
    s.close()
    return job


class TestPostOutcomeEvent:
    def test_viewed_event_returns_201(self, client: TestClient, seeded_job: Job) -> None:
        resp = client.post(f"/api/jobs/{seeded_job.id}/events", json={"event_type": "viewed"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["job_id"] == seeded_job.id
        assert body["event_type"] == "viewed"
        assert body["id"]
        assert body["occurred_at"]

    def test_apply_link_opened_event_returns_201(self, client: TestClient, seeded_job: Job) -> None:
        resp = client.post(
            f"/api/jobs/{seeded_job.id}/events", json={"event_type": "apply_link_opened"}
        )
        assert resp.status_code == 201
        assert resp.json()["event_type"] == "apply_link_opened"

    def test_invalid_event_type_returns_422(self, client: TestClient, seeded_job: Job) -> None:
        resp = client.post(f"/api/jobs/{seeded_job.id}/events", json={"event_type": "bogus"})
        assert resp.status_code == 422

    def test_application_status_changed_rejected_as_client_input(
        self, client: TestClient, seeded_job: Job
    ) -> None:
        # Clients may never directly fabricate this event type — it is
        # derived server-side from the application-status endpoint only.
        resp = client.post(
            f"/api/jobs/{seeded_job.id}/events",
            json={"event_type": "application_status_changed"},
        )
        assert resp.status_code == 422

    def test_missing_job_returns_404(self, client: TestClient) -> None:
        resp = client.post("/api/jobs/does-not-exist/events", json={"event_type": "viewed"})
        assert resp.status_code == 404

    def test_repeated_viewed_events_all_recorded(self, client: TestClient, seeded_job: Job) -> None:
        client.post(f"/api/jobs/{seeded_job.id}/events", json={"event_type": "viewed"})
        client.post(f"/api/jobs/{seeded_job.id}/events", json={"event_type": "viewed"})
        events = client.get(f"/api/jobs/{seeded_job.id}/events").json()
        assert len(events) == 2


class TestGetOutcomeEvents:
    def test_empty_when_no_events_recorded(self, client: TestClient, seeded_job: Job) -> None:
        resp = client.get(f"/api/jobs/{seeded_job.id}/events")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_recorded_events(self, client: TestClient, seeded_job: Job) -> None:
        client.post(f"/api/jobs/{seeded_job.id}/events", json={"event_type": "viewed"})
        resp = client.get(f"/api/jobs/{seeded_job.id}/events")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 1
        assert events[0]["event_type"] == "viewed"

    def test_missing_job_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/jobs/does-not-exist/events")
        assert resp.status_code == 404


class TestApplicationStatusChangeEmitsEvent:
    def test_status_change_recorded_as_event(self, client: TestClient, seeded_job: Job) -> None:
        resp = client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": "applied"},
        )
        assert resp.status_code == 200  # existing response contract unchanged
        events = client.get(f"/api/jobs/{seeded_job.id}/events").json()
        assert len(events) == 1
        assert events[0]["event_type"] == "application_status_changed"
        assert events[0]["metadata"] == {"from": None, "to": "applied"}

    def test_setting_same_status_twice_does_not_duplicate_event(
        self, client: TestClient, seeded_job: Job
    ) -> None:
        client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": "applied"},
        )
        client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": "applied"},
        )
        events = client.get(f"/api/jobs/{seeded_job.id}/events").json()
        assert len(events) == 1

    def test_changing_status_again_records_a_second_event(
        self, client: TestClient, seeded_job: Job
    ) -> None:
        client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": "applied"},
        )
        client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": "interview"},
        )
        events = client.get(f"/api/jobs/{seeded_job.id}/events").json()
        assert len(events) == 2
        assert events[0]["metadata"] == {"from": "applied", "to": "interview"}

    def test_clearing_status_to_null_records_event(self, client: TestClient, seeded_job: Job) -> None:
        client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": "applied"},
        )
        client.patch(
            f"/api/jobs/{seeded_job.id}/application-status",
            json={"application_status": None},
        )
        events = client.get(f"/api/jobs/{seeded_job.id}/events").json()
        assert len(events) == 2
        assert events[0]["metadata"] == {"from": "applied", "to": None}
