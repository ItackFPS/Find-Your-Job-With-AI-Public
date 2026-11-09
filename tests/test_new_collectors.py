"""Tests for job collectors: WorkingNomads and registry."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.registry import CollectorRegistry
from src.collectors.workingnomads import WorkingNomadsCollector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(body: Any, status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    if isinstance(body, (dict, list)):
        mock.text = json.dumps(body)
        mock.json.return_value = body
    else:
        mock.text = str(body)
        mock.json.return_value = body
    mock.raise_for_status = MagicMock()
    return mock


SAMPLE_WORKINGNOMADS_JOB: dict[str, Any] = {
    "url": "https://www.workingnomads.com/job/go/999/",
    "title": "Full Stack Developer",
    "company_name": "RemoteStartup",
    "description": "Build our platform remotely",
    "category_name": "development",
    "tags": "python, javascript, react",
    "location": "Worldwide",
    "pub_date": "2026-08-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_new_collectors_registered(self) -> None:
        reg = CollectorRegistry()
        names = reg.list_collectors()
        assert "remoteok" not in names
        assert "jobicy" not in names
        assert "weworkremotely" not in names
        assert "workingnomads" in names
        assert "bundesagentur" in names
        assert "arbetsformedlingen" in names
        assert "smartrecruiters" in names
        # rss alias must also be registered
        assert "rss" in names

    def test_rss_alias_is_remote_collector(self) -> None:
        from src.collectors.remote import RemoteCollector
        reg = CollectorRegistry()
        assert reg.get_collector_class("rss") is RemoteCollector


# ---------------------------------------------------------------------------
# WorkingNomads
# ---------------------------------------------------------------------------

class TestWorkingNomadsCollector:
    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_WORKINGNOMADS_JOB])):
            result = WorkingNomadsCollector().collect()

        assert result.source_name == "workingnomads"
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Full Stack Developer"
        assert job["company"] == "RemoteStartup"
        assert job["remote"] is True
        assert "python" in job["skills"]

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_empty_response(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([])):
            result = WorkingNomadsCollector().collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_network_error(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   side_effect=RuntimeError("timeout")):
            result = WorkingNomadsCollector().collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "timeout" in result.errors[0]

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_single_request_no_category_loop(self, _mock_rl: MagicMock) -> None:
        # Verifies the fix for the ~32s multi-category bottleneck: the
        # collector must make exactly one HTTP request per run now that the
        # API's category filter has been confirmed to be a no-op.
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_WORKINGNOMADS_JOB])) as mock_fetch:
            WorkingNomadsCollector().collect()
        assert mock_fetch.call_count == 1

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_max_jobs(self, _mock_rl: MagicMock) -> None:
        extra_job = dict(SAMPLE_WORKINGNOMADS_JOB, url="https://www.workingnomads.com/job/go/1000/")
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_WORKINGNOMADS_JOB, extra_job])):
            result = WorkingNomadsCollector(config={"max_jobs": 1}).collect()
        assert len(result.jobs) == 1

    def test_normalize_fields(self) -> None:
        out = WorkingNomadsCollector._normalize_fields(SAMPLE_WORKINGNOMADS_JOB)
        assert out["title"] == "Full Stack Developer"
        assert out["company"] == "RemoteStartup"
        assert out["remote"] is True
        assert "python" in out["skills"]
        assert "javascript" in out["skills"]
