"""Tests for the mandatory pipeline preflight (src/pipeline/preflight.py).

Covers:
  - France Travail: credentials found/missing, auth success/failure,
    expired-token refresh (reuses the collector's own OAuth logic),
    secrets never logged.
  - Overall status: France Travail credentials that fail to authenticate skip
    France Travail for the run with the failure class (DEGRADED, #303); with no
    credentials set at all it is skipped for the run (DEGRADED) too.
  - Pipeline wiring: preflight runs before collection, continues on DEGRADED
    (France Travail not collected from, the other sources still run), runs
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
    def test_france_travail_auth_failure_skips_it_and_other_sources_still_run(self, tmp_path, monkeypatch):
        """Rejected France Travail credentials no longer abort the run (#303): France Travail is
        skipped, never collected from, with the failure class named; France Travail web still collects."""
        from src.collectors.base import CollectorResult
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "sources.yaml").write_text(
            "sources:\n"
            "  - {name: france_travail, type: france_travail, active: true, config: {}}\n"
            "  - {name: france_travail_web, type: france_travail_web, active: true, config: {}}\n",
            encoding="utf-8",
        )

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5
        web_result = CollectorResult(source_name="france_travail_web", jobs=[], errors=[], duration_seconds=0.1)
        with patch(
            "src.collectors.france_travail.FranceTravailCollector.collect"
        ) as ft_collect, patch(
            "src.collectors.france_travail_web.FranceTravailWebCollector.collect", return_value=web_result
        ) as web_collect, patch.object(
            httpx.Client, "post", side_effect=_http_error_response(401)
        ), patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        ft_collect.assert_not_called()
        web_collect.assert_called_once()
        assert result.status == "completed", result.error
        assert result.stats.preflight["status"] == "DEGRADED"
        assert result.stats.preflight["ready"] is True
        assert result.stats.preflight["france_travail"]["reason_key"] == "ft_auth_invalid_client"
        skipped = result.stats.by_source["france_travail"]
        assert skipped["status"] == "skipped"
        assert skipped["reason_key"] == "ft_auth_invalid_client"
        assert "invalid_client" in skipped["reason"]
        # A failed login is a run error (unlike an unconfigured API), never a silent gap.
        assert any("France Travail skipped" in e for e in result.stats.errors)
        assert SECRET not in "; ".join(result.stats.errors)

    def test_france_travail_outage_is_retried_then_skipped(self, tmp_path, monkeypatch):
        """A maintenance redirect from the login service is retried, then skipped as an outage."""
        import src.pipeline.preflight as preflight_mod

        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)
        sleeps: list[float] = []
        monkeypatch.setattr(preflight_mod.time, "sleep", sleeps.append)
        redirect = httpx.Response(302, request=httpx.Request("POST", "https://x"),
                                  headers={"Location": "https://maintenance.example/"}, text="")

        with patch.object(httpx.Client, "post", return_value=redirect) as mock_post:
            result = run_preflight(str(tmp_path))

        assert mock_post.call_count == 1 + len(preflight_mod._TRANSIENT_RETRY_DELAYS)
        assert sleeps == list(preflight_mod._TRANSIENT_RETRY_DELAYS)
        assert (result.status, result.ready) == ("DEGRADED", True)
        assert result.france_travail.reason_key == "ft_outage"
        assert result.skip_reasons["france_travail"] == preflight_mod.FT_AUTH_SKIP_REASONS["ft_outage"]

    def test_france_travail_outage_recovering_on_retry_is_ready(self, tmp_path, monkeypatch):
        import src.pipeline.preflight as preflight_mod

        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)
        monkeypatch.setattr(preflight_mod.time, "sleep", lambda _s: None)
        outage = httpx.Response(503, request=httpx.Request("POST", "https://x"), text="down")

        with patch.object(httpx.Client, "post", side_effect=[outage, _token_response()]):
            result = run_preflight(str(tmp_path))

        assert (result.status, result.ready) == ("READY", True)
        assert result.skipped_source_types == []

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
        # a user-facing reason: never a variable name to set in a file (D4, ADR 0028)
        assert skipped["reason"].startswith("Not required.") and "FRANCE_TRAVAIL_CLIENT_ID" not in skipped["reason"]
        assert not any("France Travail" in e for e in result.stats.errors)  # normal state, not an error

    def test_indeed_not_chosen_is_skipped_visibly_and_never_called(self, tmp_path, monkeypatch):
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
        assert result.stats.preflight["status"] == "READY"        # off by choice: not degraded
        assert result.stats.by_source["indeed"]["status"] == "skipped"
        assert "unofficial source" in result.stats.by_source["indeed"]["reason"]

    def test_indeed_chosen_in_settings_next_to_the_database_runs(self, tmp_path, monkeypatch):
        from src.collectors.base import CollectorResult
        from src.pipeline.orchestrator import run_pipeline

        monkeypatch.delenv("INDEED_API_KEY", raising=False)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "sources.yaml").write_text(
            "sources:\n  - {name: indeed, type: indeed, active: true}\n", encoding="utf-8")
        (tmp_path / "settings.json").write_text('{"unofficial_sources": {"indeed": true}}', encoding="utf-8")

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        with patch("src.collectors.indeed.IndeedCollector.collect",
                   return_value=CollectorResult(source_name="indeed", jobs=[], errors=[])) as mock_collect, patch(
            "src.embeddings.embedding.EmbeddingService", return_value=mock_embed
        ):
            result = run_pipeline(db_path=str(tmp_path / "test.db"), config_dir=str(config_dir))

        mock_collect.assert_called_once()
        assert result.stats.preflight["skipped_source_types"] == []

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

    def test_active_indeed_not_chosen_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INDEED_API_KEY", raising=False)
        self._sources(tmp_path, active=True)
        result = run_preflight(str(tmp_path))
        assert (result.status, result.ready) == ("READY", True)    # the default, not a degradation
        assert result.skipped_source_types == ["indeed"]
        assert "unofficial source" in result.skip_reasons["indeed"]
        assert any("Indeed skipped" in line for line in result.report_lines)
        assert not result.errors and not result.degraded_errors

    def test_indeed_chosen_legacy_key_or_inactive_is_not_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INDEED_API_KEY", SECRET)            # an older install's setup: chosen
        self._sources(tmp_path, active=True)
        result = run_preflight(str(tmp_path))
        assert result.skipped_source_types == [] and result.status == "READY"
        assert SECRET not in "\n".join(result.report_lines) + str(result.to_dict())
        monkeypatch.delenv("INDEED_API_KEY")
        self._sources(tmp_path, active=False)
        assert run_preflight(str(tmp_path)).skipped_source_types == []

    def test_stored_choice_wins_over_the_legacy_key(self, tmp_path, monkeypatch):
        self._sources(tmp_path, active=True)
        settings = tmp_path / "settings.json"
        settings.write_text('{"unofficial_sources": {"indeed": true}}', encoding="utf-8")
        monkeypatch.delenv("INDEED_API_KEY", raising=False)
        assert run_preflight(str(tmp_path), settings_path=settings).skipped_source_types == []
        settings.write_text('{"unofficial_sources": {"indeed": false}}', encoding="utf-8")
        monkeypatch.setenv("INDEED_API_KEY", SECRET)
        assert run_preflight(str(tmp_path), settings_path=settings).skipped_source_types == ["indeed"]


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

    def test_france_travail_failure_is_degraded_and_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "id-123")
        monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", SECRET)
        _write_sources_yaml(tmp_path)

        with patch.object(httpx.Client, "post", side_effect=_http_error_response(401)):
            result = run_preflight(str(tmp_path))

        assert result.status == "DEGRADED"
        assert result.ready is True
        assert result.errors == []
        assert result.france_travail.status == "AUTH_FAILED"
        assert result.skipped_source_types == ["france_travail"]
        assert any("invalid_client" in e for e in result.degraded_errors)
        report = "\n".join(result.report_lines)
        assert "Pipeline aborted." not in report
        assert "skipped for this run" in report

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
        assert result.degraded_errors == []  # normal state of a new install, not a run error
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
