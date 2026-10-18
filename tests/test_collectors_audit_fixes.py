"""Tests for collector audit fix: weworkremotely-rss.

RemoteCollector (used for `type: rss` sources) previously called
https://remotework.com/api/v1/jobs regardless of the configured
`careers_url`. This test verifies the collector is now configuration-driven.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.collectors.remote import RemoteCollector
from src.pipeline.orchestrator import _build_collector_config

SAMPLE_RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>We Work Remotely</title>
<link>https://weworkremotely.com</link>
<item>
<title>Acme Corp: Senior Backend Engineer</title>
<link>https://weworkremotely.com/jobs/12345</link>
<guid>https://weworkremotely.com/jobs/12345</guid>
<description>&lt;p&gt;We are looking for a backend engineer.&lt;/p&gt;</description>
<category>Programming</category>
</item>
<item>
<title>Untitled Listing Without A Colon</title>
<link>https://weworkremotely.com/jobs/67890</link>
<guid>https://weworkremotely.com/jobs/67890</guid>
<description>Some role.</description>
</item>
</channel>
</rss>"""


def _mock_response(text: str = "", json_data: Any = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = text
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


# -- Issue 2: weworkremotely-rss / RemoteCollector -----------------------------


class TestRemoteCollectorUsesConfiguredCareersUrl:
    def test_rss_type_fetches_configured_careers_url(self) -> None:
        config = {
            "type": "rss",
            "careers_url": "https://www.weworkremotely.com/jobs/rss",
        }
        collector = RemoteCollector(config=config)

        with patch.object(
            collector, "_fetch_with_retry", return_value=_mock_response(text=SAMPLE_RSS_FEED)
        ) as mock_fetch:
            result = collector.collect()

        mock_fetch.assert_called_once()
        called_url = mock_fetch.call_args.args[0]
        assert called_url == "https://www.weworkremotely.com/jobs/rss"
        assert "remotework.com" not in called_url

        assert result.errors == []
        assert len(result.jobs) == 2

    def test_rss_entries_are_parsed_into_generic_job_dicts(self) -> None:
        config = {"type": "rss", "careers_url": "https://example.com/jobs/rss"}
        collector = RemoteCollector(config=config)

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(text=SAMPLE_RSS_FEED)):
            result = collector.collect()

        first = result.jobs[0]
        assert first["company"] == "Acme Corp"
        assert first["title"] == "Senior Backend Engineer"
        assert "backend engineer" in first["description"].lower()
        assert first["remote"] is True
        assert first["url"] == "https://weworkremotely.com/jobs/12345"
        assert first["categories"] == ["Programming"]

        # No "Company: Title" pattern -> whole string used as title, no crash
        second = result.jobs[1]
        assert second["title"] == "Untitled Listing Without A Colon"
        assert second["company"] == ""

    def test_rss_never_uses_the_old_hardcoded_remotework_url(self) -> None:
        config = {"type": "rss", "careers_url": "https://www.weworkremotely.com/jobs/rss"}
        collector = RemoteCollector(config=config)

        with patch("httpx.get", return_value=_mock_response(text=SAMPLE_RSS_FEED)) as mock_get:
            collector.collect()

        called_url = mock_get.call_args.args[0]
        assert called_url == "https://www.weworkremotely.com/jobs/rss"
        assert called_url != "https://remotework.com/api/v1/jobs"

    def test_rss_without_careers_url_skips_gracefully_without_crashing(self) -> None:
        collector = RemoteCollector(config={"type": "rss"})
        result = collector.collect()

        assert result.jobs == []
        assert any("careers_url" in e for e in result.errors)

    def test_ssl_verification_is_never_disabled(self) -> None:
        config = {"type": "rss", "careers_url": "https://www.weworkremotely.com/jobs/rss"}
        collector = RemoteCollector(config=config)

        with patch("httpx.get", return_value=_mock_response(text=SAMPLE_RSS_FEED)) as mock_get:
            collector.collect()

        _args, kwargs = mock_get.call_args
        assert kwargs.get("verify", True) is not False

    def test_non_rss_type_falls_back_to_json_api_using_configured_url(self) -> None:
        """Non-RSS 'remote' sources should still work as a JSON API, driven
        by the configured careers_url -- REST/API support is preserved."""
        config = {"type": "remote", "careers_url": "https://example.com/api/jobs"}
        collector = RemoteCollector(config=config)
        payload = [{"title": "Support Engineer", "company": {"name": "Widgets Inc"}}]

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(json_data=payload)) as mock_fetch:
            result = collector.collect()

        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.args[0] == "https://example.com/api/jobs"
        assert len(result.jobs) == 1

    def test_json_api_without_careers_url_skips_gracefully(self) -> None:
        collector = RemoteCollector(config={"type": "remote"})
        result = collector.collect()

        assert result.jobs == []
        assert any("careers_url" in e for e in result.errors)


class TestMainCollectorConfigWiring:
    """Verify src/main.py actually forwards careers_url/board_identifier to
    collectors instead of only the previously-used nested "config" dict."""

    def test_build_collector_config_includes_top_level_fields(self) -> None:
        source_config = {
            "name": "weworkremotely-rss",
            "type": "rss",
            "careers_url": "https://www.weworkremotely.com/jobs/rss",
            "board_identifier": None,
            "config": {},
        }
        merged = _build_collector_config(source_config)

        assert merged["careers_url"] == "https://www.weworkremotely.com/jobs/rss"
        assert merged["type"] == "rss"

    def test_nested_config_overrides_top_level_on_collision(self) -> None:
        source_config = {
            "name": "x",
            "careers_url": "https://top-level.example/jobs",
            "config": {"careers_url": "https://nested.example/jobs"},
        }
        merged = _build_collector_config(source_config)
        assert merged["careers_url"] == "https://nested.example/jobs"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
