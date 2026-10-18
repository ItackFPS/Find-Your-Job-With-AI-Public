"""Tests for src.api.auth.require_n8n_api_key — the opt-in shared-secret
check protecting only the n8n-facing endpoints.

Covers:
- Disabled by default (N8N_CALLER_API_KEY unset) — every existing local/dev
  call keeps working with no header at all.
- Enabled (N8N_CALLER_API_KEY set): correct key -> allowed, wrong/missing
  key -> 401.
- Scope stays narrow: an unrelated, non-n8n-facing endpoint in the SAME
  router never requires the key, even when N8N_CALLER_API_KEY is set —
  proves the dependency was applied per-route, not at the router level.
- N8N_CALLER_API_KEY is intentionally NOT named N8N_API_KEY: this project's
  .env separately defines N8N_API_KEY for n8n's OWN server/public API (an
  unrelated, opposite-direction credential) — reusing that name would
  silently pick up whatever value is already sitting there.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.notifications.engine import NotificationEngine
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


@pytest.fixture()
def tmp_db(tmp_path):
    db = str(tmp_path / "test.db")
    s = Storage(db_path=db)
    s.init_db()
    s.close()
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.close()
    return db


@pytest.fixture()
def client(tmp_db):
    app.state.db_path = tmp_db
    app.state.config_dir = str(Path("config"))
    # app.state.notification_engine defaults to a module-level singleton
    # wired to the real data/jobs.db (see src/api/app.py) — override it here
    # too, or GET /notifications/status would open the real production db
    # instead of this test's isolated tmp_db.
    app.state.notification_engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path("config")))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_api_key_env(monkeypatch):
    """Auth must be OFF by default in every test unless a test explicitly
    opts in — prevents a leaked N8N_CALLER_API_KEY from an earlier test (or
    the real environment) silently changing these tests' behavior."""
    monkeypatch.delenv("N8N_CALLER_API_KEY", raising=False)


class TestDisabledByDefault:
    """N8N_CALLER_API_KEY unset — every protected endpoint must keep
    working with zero header, exactly as before this feature existed."""

    def test_weekly_digest_works_with_no_header(self, client):
        resp = client.get("/api/jobs/digest/weekly")
        assert resp.status_code == 200

    def test_weekly_digest_combined_works_with_no_header_even_with_no_run(self, client):
        resp = client.get("/api/jobs/digest/weekly-combined")
        # 503 (no completed run) is the EXPECTED unauthenticated response
        # here — the point is it's not a 401.
        assert resp.status_code != 401

    def test_n8n_job_listing_works_with_no_header(self, client, tmp_db):
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"personalized": 0})
        rs.close()
        resp = client.get(f"/api/pipeline/runs/{run_id}/jobs/n8n")
        assert resp.status_code == 200

    def test_arbitrary_header_value_is_still_accepted_when_disabled(self, client):
        """Disabled means disabled — an unexpected header must not somehow
        trigger a check."""
        resp = client.get("/api/jobs/digest/weekly", headers={"X-N8N-API-Key": "whatever"})
        assert resp.status_code == 200


class TestDoesNotCollideWithN8nsOwnApiKey:
    """Regression: this project's .env separately defines N8N_API_KEY for
    n8n's OWN server/public API (an unrelated, opposite-direction
    credential — confirmed present locally as N8N_HOST/N8N_PORT/
    N8N_PROTOCOL/N8N_API_KEY). An earlier implementation pass named this
    feature's variable N8N_API_KEY too, which meant setting up n8n locally
    silently "enabled" this backend's auth using n8n's own placeholder key
    — breaking every existing unauthenticated call with no code change on
    this project's side at all. N8N_CALLER_API_KEY must never read from
    N8N_API_KEY, in either direction."""

    def test_setting_the_unrelated_n8n_api_key_does_not_enable_auth(self, client, monkeypatch):
        monkeypatch.setenv("N8N_API_KEY", "your-n8n-api-key-here")
        resp = client.get("/api/jobs/digest/weekly")
        assert resp.status_code == 200

    def test_sending_n8n_api_key_value_as_the_header_is_not_treated_as_authoritative(
        self, client, monkeypatch
    ):
        """Even if a caller mistakenly sends n8n's own key as the header
        value, that must never coincidentally satisfy N8N_CALLER_API_KEY
        (proven separately: they are different env vars entirely, but this
        pins the end-to-end behavior too)."""
        monkeypatch.setenv("N8N_API_KEY", "your-n8n-api-key-here")
        monkeypatch.setenv("N8N_CALLER_API_KEY", "the-real-caller-secret")
        resp = client.get(
            "/api/jobs/digest/weekly", headers={"X-N8N-API-Key": "your-n8n-api-key-here"}
        )
        assert resp.status_code == 401


class TestEnabledRequiresCorrectKey:
    def test_correct_key_is_allowed(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/jobs/digest/weekly", headers={"X-N8N-API-Key": "secret-123"})
        assert resp.status_code == 200

    def test_missing_header_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/jobs/digest/weekly")
        assert resp.status_code == 401

    def test_wrong_key_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/jobs/digest/weekly", headers={"X-N8N-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_empty_header_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/jobs/digest/weekly", headers={"X-N8N-API-Key": ""})
        assert resp.status_code == 401

    def test_weekly_combined_endpoint_also_enforces_the_key(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        rejected = client.get("/api/jobs/digest/weekly-combined")
        assert rejected.status_code == 401
        allowed = client.get(
            "/api/jobs/digest/weekly-combined", headers={"X-N8N-API-Key": "secret-123"}
        )
        assert allowed.status_code != 401  # 503 (no run) is fine — the point is it's past auth

    def test_n8n_job_listing_endpoint_also_enforces_the_key(self, client, monkeypatch, tmp_db):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"personalized": 0})
        rs.close()

        rejected = client.get(f"/api/pipeline/runs/{run_id}/jobs/n8n")
        assert rejected.status_code == 401
        allowed = client.get(
            f"/api/pipeline/runs/{run_id}/jobs/n8n", headers={"X-N8N-API-Key": "secret-123"}
        )
        assert allowed.status_code == 200


class TestScopeStaysNarrow:
    """The dependency was applied per-route, never at the router level —
    an unrelated endpoint in the SAME router must never require the key,
    even once N8N_CALLER_API_KEY is set."""

    def test_list_jobs_never_requires_the_key(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/jobs")
        assert resp.status_code != 401

    def test_job_stats_never_requires_the_key(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/jobs/stats")
        assert resp.status_code != 401

    def test_notification_status_never_requires_the_key(self, client, monkeypatch):
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        resp = client.get("/api/pipeline/notifications/status")
        assert resp.status_code != 401

    def test_run_jobs_non_n8n_variant_never_requires_the_key(self, client, monkeypatch, tmp_db):
        """GET /runs/{id}/jobs (the pre-existing, non-n8n endpoint) must
        stay open even though its n8n sibling /runs/{id}/jobs/n8n now
        requires the key — proves the two were not accidentally conflated."""
        monkeypatch.setenv("N8N_CALLER_API_KEY", "secret-123")
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"personalized": 0})
        rs.close()
        resp = client.get(f"/api/pipeline/runs/{run_id}/jobs")
        assert resp.status_code != 401
