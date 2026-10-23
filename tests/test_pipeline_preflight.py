"""Tests for the mandatory pipeline preflight (src/pipeline/preflight.py).

Covers:
  - France Travail: credentials found/missing, auth success/failure,
    expired-token refresh (reuses the collector's own OAuth logic),
    secrets never logged.
  - Overall status: France Travail is a collection source and blocks the
    run (FAILED) when its credentials fail to authenticate; with no
    credentials set at all it is skipped for the run (DEGRADED).
  - Pipeline wiring: preflight runs before collection, aborts the run only
    on a FAILED (France Travail) status, continues on DEGRADED (an
    unconfigured France Travail that is then not collected from), runs
    exactly once, and does not change existing behavior when the check is
    healthy or skipped.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.pipeline.preflight import (
    _check_france_travail,
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
# Pipeline wiring
# ---------------------------------------------------------------------------

class TestPipelinePreflightWiring:
    def test_preflight_failure_aborts_before_collection(self, tmp_path, monkeypatch):
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        config_dir = tmp_path / "config"
        _write_sources_yaml(config_dir)  # active france_travail, rejected credentials

        with patch(
            "src.collectors.france_travail.FranceTravailCollector.collect"
        ) as mock_collect, patch.object(
            httpx.Client, "post", side_effect=_http_error_response(401)
        ):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        mock_collect.assert_not_called()
        assert result.status == "failed"
        assert "Preflight failed" in (result.error or "")
        assert result.stats.collected == 0
        assert result.stats.preflight.get("ready") is False
        assert result.stats.preflight.get("status") == "FAILED"

    def test_france_travail_not_silently_skipped_when_credentials_are_rejected(self, tmp_path, monkeypatch):
        """Configured credentials that fail to authenticate fail the run fast
        rather than producing a scan silently missing that source."""
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        config_dir = tmp_path / "config"
        _write_sources_yaml(config_dir)

        with patch.object(httpx.Client, "post", side_effect=_http_error_response(401)):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        assert result.status == "failed"
        assert "France Travail" in (result.error or "")

    def test_unconfigured_france_travail_is_skipped_visibly_and_run_continues(self, tmp_path, monkeypatch):
        """A fresh install has no France Travail credentials: the run goes on
        without that source (never calling it), and says so in its errors."""
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        config_dir = tmp_path / "config"
        _write_sources_yaml(config_dir)

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5
        with patch(
            "src.collectors.france_travail.FranceTravailCollector.collect"
        ) as mock_collect, patch.object(httpx.Client, "post") as mock_post, patch(
            "src.embeddings.embedding.EmbeddingService", return_value=mock_embed
        ):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        mock_collect.assert_not_called()
        mock_post.assert_not_called()
        assert result.status == "completed", f"Pipeline failed: {result.error}"
        assert result.stats.preflight.get("status") == "DEGRADED"
        assert result.stats.preflight.get("skipped_source_types") == ["france_travail"]
        # listed as skipped, with what to set up -- never as a source that ran
        skipped = result.stats.by_source["france_travail"]
        assert (skipped["status"], skipped["collected"], skipped["errors"]) == ("skipped", 0, 0)
        assert "FRANCE_TRAVAIL_CLIENT_ID" in skipped["reason"]
        assert any("France Travail skipped" in e for e in result.stats.errors)

    def test_indeed_without_api_key_is_skipped_visibly_and_never_called(self, tmp_path, monkeypatch):
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.delenv("INDEED_API_KEY", raising=False)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "sources.yaml").write_text(
            "sources:\n  - {name: indeed, type: indeed, active: true}\n", encoding="utf-8")

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        with patch("src.collectors.indeed.IndeedCollector.collect") as mock_collect, patch(
            "src.embeddings.embedding.EmbeddingService", return_value=mock_embed
        ):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        mock_collect.assert_not_called()
        assert result.status == "completed", result.error
        assert result.stats.preflight["skipped_source_types"] == ["indeed"]
        assert result.stats.by_source["indeed"]["status"] == "skipped"
        assert "INDEED_API_KEY" in result.stats.by_source["indeed"]["reason"]
        assert any(e.startswith("Indeed skipped for this run") for e in result.stats.errors)

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
        """No sources.yaml -> the check is SKIPPED ->
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

# ---------------------------------------------------------------------------
# Overall preflight status (FAILED vs DEGRADED vs READY)
# ---------------------------------------------------------------------------

class TestIndeedPreflight:
    @staticmethod
    def _sources(config_dir: Path, *, active: bool) -> None:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "sources.yaml").write_text(
            f"sources:\n  - {{name: indeed, type: indeed, active: {str(active).lower()}}}\n", encoding="utf-8")

    def test_active_indeed_without_key_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INDEED_API_KEY", raising=False)
        self._sources(tmp_path, active=True)
        result = run_preflight(str(tmp_path))
        assert (result.status, result.ready) == ("DEGRADED", True)
        assert result.skipped_source_types == ["indeed"]
        assert "INDEED_API_KEY" in result.skip_reasons["indeed"]
        assert ".env" in result.skip_reasons["indeed"]
        assert any("Indeed skipped" in line for line in result.report_lines)
        assert not result.errors  # optional source: never blocks the run

    def test_indeed_with_key_or_inactive_is_not_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INDEED_API_KEY", SECRET)
        self._sources(tmp_path, active=True)
        result = run_preflight(str(tmp_path))
        assert result.skipped_source_types == [] and result.status == "READY"
        assert SECRET not in "\n".join(result.report_lines) + str(result.to_dict())
        monkeypatch.delenv("INDEED_API_KEY")
        self._sources(tmp_path, active=False)
        assert run_preflight(str(tmp_path)).skipped_source_types == []


class TestOverallPreflightStatus:
    def test_healthy_france_travail_is_ready(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)

        with patch.object(httpx.Client, "post", return_value=_token_response()):
            result = run_preflight(str(tmp_path))

        assert result.status == "READY"
        assert result.ready is True
        assert result.france_travail.status == "READY"

    def test_france_travail_failure_is_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)

        with patch.object(httpx.Client, "post", side_effect=_http_error_response(401)):
            result = run_preflight(str(tmp_path))

        assert result.status == "FAILED"
        assert result.ready is False
        assert result.france_travail.status == "AUTH_FAILED"
        assert result.skipped_source_types == []

    def test_unconfigured_france_travail_is_degraded_not_failed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
        monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
        _write_sources_yaml(tmp_path)

        result = run_preflight(str(tmp_path))

        assert result.status == "DEGRADED"
        assert result.ready is True
        assert result.errors == []
        assert result.france_travail.status == "CONFIG_MISSING"
        assert result.skipped_source_types == ["france_travail"]
        assert any("France Travail skipped" in e for e in result.degraded_errors)
        report = "\n".join(result.report_lines)
        assert "skipped for this run" in report
        assert "Pipeline aborted." not in report

    def test_disabled_integrations_remain_skipped_and_ready(self, tmp_path):
        config_dir = tmp_path
        config_dir.mkdir(exist_ok=True)

        result = run_preflight(str(config_dir))

        assert result.status == "READY"
        assert result.ready is True
        assert result.france_travail.status == "SKIPPED"
