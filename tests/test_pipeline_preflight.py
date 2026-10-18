"""Tests for the mandatory pipeline preflight (src/pipeline/preflight.py).

Covers:
  - France Travail: credentials found/missing, auth success/failure,
    expired-token refresh (reuses the collector's own OAuth logic),
    secrets never logged.
  - n8n: server reachable/unreachable, webhook registered/unregistered,
    disabled n8n is skipped, existing config (env + yaml) is reused.
  - Overall status: France Travail is a collection source and blocks the
    run (FAILED) on missing creds/auth failure. n8n is downstream
    notification infrastructure and only degrades the run (DEGRADED) —
    it never blocks collection/filtering/scoring/ranking.
  - Pipeline wiring: preflight runs before collection, aborts the run only
    on a FAILED (France Travail) status, continues on DEGRADED (n8n),
    records the n8n failure in the final stats, runs exactly once, and
    does not change existing behavior when both checks are healthy or
    skipped.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.pipeline.preflight import (
    _check_france_travail,
    _check_n8n,
    _probe_indicates_registered,
    run_preflight,
)

SECRET = "super-secret-value-should-never-be-logged"


def _write_sources_yaml(config_dir: Path, *, active: bool = True) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "sources.yaml").write_text(
        f"""
sources:
  - name: france_travail
    type: france_travail
    active: {str(active).lower()}
    config: {{}}
""",
        encoding="utf-8",
    )


def _write_notifications_yaml(config_dir: Path, *, enabled: bool, webhook_url: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "notifications.yaml").write_text(
        f"""
notifications:
  enabled: true
  n8n:
    enabled: {str(enabled).lower()}
    webhook_url: "{webhook_url}"
""",
        encoding="utf-8",
    )


def _token_response(access_token: str = "tok-123", expires_in: int = 1440) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"access_token": access_token, "expires_in": expires_in}
    return resp


def _http_error_response(status_code: int) -> httpx.HTTPStatusError:
    resp = httpx.Response(status_code, request=httpx.Request("POST", "https://x"), text="invalid_client")
    return httpx.HTTPStatusError("error", request=resp.request, response=resp)


# ---------------------------------------------------------------------------
# France Travail
# ---------------------------------------------------------------------------

class TestFranceTravailPreflight:
    def test_credentials_missing_is_config_missing_and_no_network_call(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        _write_sources_yaml(tmp_path)

        with patch.object(httpx.Client, "post") as mock_post:
            result = _check_france_travail(str(tmp_path))

        assert result.status == "CONFIG_MISSING"
        assert result.credentials == "MISSING"
        assert result.authentication == "SKIPPED"
        mock_post.assert_not_called()

    def test_credentials_present_authentication_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)

        with patch.object(httpx.Client, "post", return_value=_token_response()):
            result = _check_france_travail(str(tmp_path))

        assert result.status == "READY"
        assert result.credentials == "FOUND"
        assert result.authentication == "OK"

    def test_authentication_failure_is_clear_and_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)

        with patch.object(httpx.Client, "post", side_effect=_http_error_response(401)):
            result = _check_france_travail(str(tmp_path))

        assert result.status == "AUTH_FAILED"
        assert result.credentials == "FOUND"
        assert result.authentication == "FAILED"
        assert result.detail is not None

    def test_inactive_source_is_skipped_not_failed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        _write_sources_yaml(tmp_path, active=False)

        result = _check_france_travail(str(tmp_path))

        assert result.status == "SKIPPED"
        assert result.enabled is False

    def test_missing_sources_yaml_is_skipped(self, tmp_path):
        result = _check_france_travail(str(tmp_path))
        assert result.status == "SKIPPED"

    def test_expired_token_is_automatically_refreshed(self, monkeypatch):
        """Reuses the collector's own _get_or_refresh_token — the same
        mechanism preflight relies on. An expired cached token must trigger
        a fresh fetch rather than being reused."""
        from src.collectors.france_travail import FranceTravailCollector

        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        collector = FranceTravailCollector()
        collector._token = "stale-token"
        collector._token_expires_at = time.time() - 100  # already expired

        with patch.object(httpx.Client, "post", return_value=_token_response("fresh-token")) as mock_post:
            with httpx.Client() as client:
                token = collector._get_or_refresh_token(client)

        mock_post.assert_called_once()
        assert token == "fresh-token"

    def test_secret_never_appears_in_logs_or_report(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)

        with caplog.at_level("DEBUG"):
            with patch.object(httpx.Client, "post", side_effect=_http_error_response(401)):
                result = run_preflight(str(tmp_path))

        assert SECRET not in "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET not in "\n".join(result.report_lines)
        assert SECRET not in "; ".join(result.errors)


# ---------------------------------------------------------------------------
# n8n
# ---------------------------------------------------------------------------

class TestN8nPreflight:
    def test_disabled_is_skipped(self, tmp_path):
        _write_notifications_yaml(tmp_path, enabled=False, webhook_url="http://localhost:5678/webhook/job-pipeline")
        result = _check_n8n(str(tmp_path))
        assert result.status == "SKIPPED"
        assert result.enabled is False

    def test_reachable_and_webhook_available_is_ready(self, tmp_path):
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")
        healthz_resp = httpx.Response(200, request=httpx.Request("GET", "http://localhost:5678/healthz"))
        webhook_resp = httpx.Response(
            404,
            json={"message": "This webhook is not registered for GET requests. Did you mean to make a POST request?"},
            request=httpx.Request("GET", "http://localhost:5678/webhook/job-pipeline"),
        )

        with patch("httpx.get", side_effect=[healthz_resp, webhook_resp]):
            result = _check_n8n(str(tmp_path))

        assert result.status == "READY"
        assert result.server == "REACHABLE"
        assert result.webhook == "AVAILABLE"

    def test_server_unreachable_is_connection_failed(self, tmp_path):
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = _check_n8n(str(tmp_path))

        assert result.status == "CONNECTION_FAILED"
        assert result.server == "UNREACHABLE"

    def test_reachable_but_webhook_missing_is_webhook_unavailable(self, tmp_path):
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")
        healthz_resp = httpx.Response(200, request=httpx.Request("GET", "http://localhost:5678/healthz"))
        webhook_resp = httpx.Response(
            404,
            json={"message": 'The requested webhook "job-pipeline" is not registered.'},
            request=httpx.Request("GET", "http://localhost:5678/webhook/job-pipeline"),
        )

        with patch("httpx.get", side_effect=[healthz_resp, webhook_resp]):
            result = _check_n8n(str(tmp_path))

        assert result.status == "WEBHOOK_UNAVAILABLE"
        assert result.server == "REACHABLE"
        assert result.webhook == "UNAVAILABLE"

    def test_webhook_auth_failure_is_not_reported_as_unregistered(self, tmp_path):
        """A 401/403 on the webhook probe is an auth/credential problem, not
        a missing workflow — the detail message must say so distinctly
        rather than reusing the generic "not registered... Activate the
        workflow" wording, which actively misdirects an operator debugging
        a real auth misconfiguration (forensic audit finding)."""
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")
        healthz_resp = httpx.Response(200, request=httpx.Request("GET", "http://localhost:5678/healthz"))
        webhook_resp = httpx.Response(
            401,
            json={"message": "Unauthorized"},
            request=httpx.Request("GET", "http://localhost:5678/webhook/job-pipeline"),
        )

        with patch("httpx.get", side_effect=[healthz_resp, webhook_resp]):
            result = _check_n8n(str(tmp_path))

        assert result.status == "WEBHOOK_UNAVAILABLE"
        assert "unauthorized" in result.detail.lower()
        assert "not registered" not in result.detail.lower()
        assert "activate the n8n workflow" not in result.detail.lower()

    def test_existing_env_var_precedence_is_reused(self, tmp_path, monkeypatch):
        """N8N_WEBHOOK_URL must win over notifications.yaml, exactly like
        the existing N8nProvider — preflight must not introduce a second
        config path."""
        monkeypatch.setenv("N8N_WEBHOOK_URL", "http://localhost:9999/webhook/other")
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")) as mock_get:
            _check_n8n(str(tmp_path))

        called_urls = [c.args[0] for c in mock_get.call_args_list]
        assert any("9999" in u for u in called_urls)

    def test_probe_heuristic_accepts_200(self):
        resp = httpx.Response(200, request=httpx.Request("GET", "http://x"))
        assert _probe_indicates_registered(resp)

    def test_probe_heuristic_rejects_unrelated_404(self):
        resp = httpx.Response(404, text="not found", request=httpx.Request("GET", "http://x"))
        assert not _probe_indicates_registered(resp)


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------

class TestPipelinePreflightWiring:
    def test_preflight_failure_aborts_before_collection(self, tmp_path, monkeypatch):
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        config_dir = tmp_path / "config"
        _write_sources_yaml(config_dir)  # active france_travail, no credentials

        with patch(
            "src.collectors.france_travail.FranceTravailCollector.collect"
        ) as mock_collect:
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        mock_collect.assert_not_called()
        assert result.status == "failed"
        assert "Preflight failed" in (result.error or "")
        assert result.stats.collected == 0
        assert result.stats.preflight.get("ready") is False
        assert result.stats.preflight.get("status") == "FAILED"

    def test_france_travail_not_silently_skipped_when_active_and_misconfigured(self, tmp_path, monkeypatch):
        """Before this change, missing credentials only appended a collector
        error and the pipeline still completed. Now it must fail fast."""
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        config_dir = tmp_path / "config"
        _write_sources_yaml(config_dir)

        result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        assert result.status == "failed"
        assert "France Travail" in (result.error or "")

    def test_preflight_runs_exactly_once_per_run(self, tmp_path):
        from src.pipeline import preflight as preflight_module
        from src.pipeline.orchestrator import run_pipeline

        with patch.object(
            preflight_module, "run_preflight", wraps=preflight_module.run_preflight
        ) as spy:
            mock_embed = MagicMock()
            mock_embed.embed.return_value = [0.1] * 384
            mock_embed.compute_semantic_score.return_value = 0.5
            mock_embed.compute_job_semantic_score.return_value = 0.5
            with patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed):
                run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(tmp_path / "config"))

        assert spy.call_count == 1

    def test_successful_preflight_does_not_change_existing_behavior(self, tmp_path):
        """No sources.yaml / notifications.yaml -> both checks SKIPPED ->
        ready=True -> pipeline proceeds and completes exactly as before
        this feature existed."""
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5

        with patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        assert result.status == "completed", f"Pipeline failed: {result.error}"
        assert result.stats.preflight.get("ready") is True
        assert result.stats.preflight.get("status") == "READY"

    def test_n8n_failure_does_not_block_pipeline(self, tmp_path):
        """n8n is downstream notification infrastructure: a reachability
        failure must be reported (DEGRADED) but must never stop collection,
        filtering, scoring, or ranking."""
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_notifications_yaml(
            config_dir, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline"
        )
        # No sources.yaml -> France Travail check is SKIPPED, isolating the
        # scenario to n8n only.

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            with patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed):
                result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        assert result.status == "completed", f"Pipeline unexpectedly failed: {result.error}"
        assert result.stats.preflight.get("ready") is True

    def test_n8n_failure_is_recorded_in_final_stats(self, tmp_path):
        """The final pipeline result must clearly show notifications could
        not be delivered, even though the run itself succeeded."""
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_notifications_yaml(
            config_dir, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline"
        )

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            with patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed):
                result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        assert result.status == "completed"
        preflight_stats = result.stats.preflight
        assert preflight_stats.get("status") == "DEGRADED"
        assert preflight_stats["n8n"]["status"] == "CONNECTION_FAILED"
        assert any("n8n" in e for e in result.stats.errors), result.stats.errors


# ---------------------------------------------------------------------------
# Overall preflight status (FAILED vs DEGRADED vs READY)
# ---------------------------------------------------------------------------

class TestOverallPreflightStatus:
    def test_both_healthy_is_ready(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")

        healthz_resp = httpx.Response(200, request=httpx.Request("GET", "http://localhost:5678/healthz"))
        webhook_resp = httpx.Response(
            404,
            json={"message": "This webhook is not registered for GET requests. Did you mean to make a POST request?"},
            request=httpx.Request("GET", "http://localhost:5678/webhook/job-pipeline"),
        )

        with patch.object(httpx.Client, "post", return_value=_token_response()):
            with patch("httpx.get", side_effect=[healthz_resp, webhook_resp]):
                result = run_preflight(str(tmp_path))

        assert result.status == "READY"
        assert result.ready is True
        assert result.france_travail.status == "READY"
        assert result.n8n.status == "READY"

    def test_france_travail_healthy_n8n_unavailable_is_degraded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")

        with patch.object(httpx.Client, "post", return_value=_token_response()):
            with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
                result = run_preflight(str(tmp_path))

        assert result.status == "DEGRADED"
        assert result.ready is True  # must not block collection
        assert result.france_travail.status == "READY"
        assert result.n8n.status == "CONNECTION_FAILED"
        assert any("n8n" in e for e in result.degraded_errors)
        assert not result.errors  # nothing blocking

    def test_france_travail_failure_is_failed_regardless_of_n8n(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        _write_sources_yaml(tmp_path)
        _write_notifications_yaml(tmp_path, enabled=True, webhook_url="http://localhost:5678/webhook/job-pipeline")

        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = run_preflight(str(tmp_path))

        assert result.status == "FAILED"
        assert result.ready is False
        assert result.france_travail.status == "CONFIG_MISSING"

    def test_disabled_integrations_remain_skipped_and_ready(self, tmp_path):
        config_dir = tmp_path
        config_dir.mkdir(exist_ok=True)

        result = run_preflight(str(config_dir))

        assert result.status == "READY"
        assert result.ready is True
        assert result.france_travail.status == "SKIPPED"
        assert result.n8n.status == "SKIPPED"
