"""Tests for Greenhouse and Lever collectors.

All tests use mocked HTTP responses — no live network calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.greenhouse import (
    GreenhouseCollector,
    _map_job as gh_map_job,
    _parse_location_countries as gh_parse_countries,
    _parse_work_mode,
)
from src.collectors.lever import (
    LeverCollector,
    _map_job as lv_map_job,
    _parse_location_countries as lv_parse_countries,
)
from src.collectors.registry import CollectorRegistry


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _mock_response(body: Any, status_code: int = 200) -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
    mock.json.return_value = body
    mock.raise_for_status = MagicMock()
    mock.headers = {}
    return mock


def _mock_404() -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = 404
    exc = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=mock)
    mock.raise_for_status.side_effect = exc
    return mock


# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------

GREENHOUSE_JOB: dict[str, Any] = {
    "id": 6141481004,
    "title": "Staff Software Engineer",
    "absolute_url": "https://job-boards.greenhouse.io/doctolib/jobs/6141481004",
    "company_name": "Doctolib",
    "location": {"name": "Paris, France"},
    "departments": [{"id": 1, "name": "Engineering"}],
    "content": "<p>Build healthcare software at scale.</p>",
    "first_published": "2026-08-01T10:00:00.000Z",
    "updated_at": "2026-08-10T08:00:00.000Z",
    "metadata": [],
}

GREENHOUSE_RESPONSE: dict[str, Any] = {
    "jobs": [GREENHOUSE_JOB],
    "meta": {"total": 1},
}

LEVER_JOB: dict[str, Any] = {
    "id": "abc-def-123",
    "text": "AI Architect",
    "categories": {
        "location": "Paris, France",
        "department": "Engineering",
        "commitment": "CDI",
        "team": "AI",
    },
    "country": "France",
    "workplaceType": "hybrid",
    "createdAt": 1690000000000,
    "descriptionPlain": "Build AI systems at Theodo.",
    "description": "<p>Build AI systems at Theodo.</p>",
    "hostedUrl": "https://jobs.lever.co/theodo/abc-def-123",
    "applyUrl": "https://jobs.lever.co/theodo/abc-def-123/apply",
    "lists": [],
    "salaryRange": {},
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistryIncludes:
    def test_greenhouse_registered(self) -> None:
        reg = CollectorRegistry()
        assert "greenhouse" in reg.list_collectors()

    def test_lever_registered(self) -> None:
        reg = CollectorRegistry()
        assert "lever" in reg.list_collectors()

    def test_greenhouse_class(self) -> None:
        reg = CollectorRegistry()
        assert reg.get_collector_class("greenhouse") is GreenhouseCollector

    def test_lever_class(self) -> None:
        reg = CollectorRegistry()
        assert reg.get_collector_class("lever") is LeverCollector


# ---------------------------------------------------------------------------
# GreenhouseCollector — unit tests
# ---------------------------------------------------------------------------

class TestGreenhouseCollector:

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_successful_collection(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            return_value=_mock_response(GREENHOUSE_RESPONSE),
        ):
            result = GreenhouseCollector(config={"board_identifiers": ["doctolib"]}).collect()

        assert result.source_name == "greenhouse"
        assert len(result.jobs) == 1
        assert result.errors == []
        assert result.listing_complete is True  # the whole board
        job = result.jobs[0]
        assert job["title"] == "Staff Software Engineer"
        assert job["company"] == "Doctolib"
        assert job["countries"] == ["France"]
        assert job["canonical_url"] == "https://job-boards.greenhouse.io/doctolib/jobs/6141481004"
        assert job["source_job_id"] == "6141481004"

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_multiple_boards_iterated(self, _rl: MagicMock) -> None:
        resp1 = _mock_response({"jobs": [GREENHOUSE_JOB], "meta": {"total": 1}})
        resp2 = _mock_response({"jobs": [dict(GREENHOUSE_JOB, id=9999, title="ML Engineer")],
                                "meta": {"total": 1}})
        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            side_effect=[resp1, resp2],
        ):
            result = GreenhouseCollector(
                config={"board_identifiers": ["doctolib", "algolia"]}
            ).collect()

        assert len(result.jobs) == 2
        assert result.errors == []

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_404_board_skipped_gracefully(self, _rl: MagicMock) -> None:
        err_mock = MagicMock(spec=httpx.Response)
        err_mock.status_code = 404
        http_exc = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=err_mock)

        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            side_effect=http_exc,
        ):
            result = GreenhouseCollector(
                config={"board_identifiers": ["nonexistent-company"]}
            ).collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "404" in result.errors[0]
        assert "nonexistent-company" in result.errors[0]

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_404_one_board_continues_others(self, _rl: MagicMock) -> None:
        """A 404 on one board does not abort collection of the next board."""
        err_mock = MagicMock(spec=httpx.Response)
        err_mock.status_code = 404
        http_exc = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=err_mock)
        ok_resp = _mock_response(GREENHOUSE_RESPONSE)

        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            side_effect=[http_exc, ok_resp],
        ):
            result = GreenhouseCollector(
                config={"board_identifiers": ["bad-token", "doctolib"]}
            ).collect()

        assert len(result.jobs) == 1
        assert len(result.errors) == 1

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_network_error_adds_error(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            side_effect=RuntimeError("connection refused"),
        ):
            result = GreenhouseCollector(config={"board_identifiers": ["doctolib"]}).collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "connection refused" in result.errors[0]
        assert result.listing_complete is False

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_max_jobs_cap(self, _rl: MagicMock) -> None:
        extra_job = dict(GREENHOUSE_JOB, id=99999, title="Backend Engineer")
        resp = _mock_response({"jobs": [GREENHOUSE_JOB, extra_job], "meta": {"total": 2}})
        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            return_value=resp,
        ):
            result = GreenhouseCollector(
                config={"board_identifiers": ["doctolib"], "max_jobs": 1}
            ).collect()

        assert len(result.jobs) == 1

    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_empty_board(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.greenhouse.GreenhouseCollector._fetch_with_retry",
            return_value=_mock_response({"jobs": [], "meta": {"total": 0}}),
        ):
            result = GreenhouseCollector(config={"board_identifiers": ["doctolib"]}).collect()

        assert result.jobs == []
        assert result.errors == []

    def test_no_config_returns_error(self) -> None:
        result = GreenhouseCollector(config={}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "board" in result.errors[0].lower()

    def test_board_identifier_merged_with_list(self) -> None:
        """board_identifier and board_identifiers are merged."""
        col = GreenhouseCollector(config={
            "board_identifier": "alpha",
            "board_identifiers": ["beta", "gamma"],
        })
        assert "alpha" in col.board_identifiers
        assert "beta" in col.board_identifiers
        assert "gamma" in col.board_identifiers


# ---------------------------------------------------------------------------
# Greenhouse pure helpers
# ---------------------------------------------------------------------------

class TestGreenhousePureHelpers:

    def test_map_job_fields(self) -> None:
        out = gh_map_job(GREENHOUSE_JOB, "doctolib")
        assert out["title"] == "Staff Software Engineer"
        assert out["company"] == "Doctolib"
        assert out["description"] == "<p>Build healthcare software at scale.</p>"
        assert out["location"] == "Paris, France"
        assert out["countries"] == ["France"]
        assert out["canonical_url"] == "https://job-boards.greenhouse.io/doctolib/jobs/6141481004"
        assert out["source_job_id"] == "6141481004"
        assert out["published_at"] == "2026-08-01T10:00:00.000Z"

    def test_map_job_company_fallback(self) -> None:
        raw = dict(GREENHOUSE_JOB)
        del raw["company_name"]
        out = gh_map_job(raw, "my-company")
        assert out["company"] == "My Company"

    def test_map_job_remote_location(self) -> None:
        raw = dict(GREENHOUSE_JOB, location={"name": "Remote - France"})
        out = gh_map_job(raw, "doctolib")
        assert out["countries"] == ["France"]
        assert out["work_mode"] == "remote"
        assert out["remote"] is True

    def test_parse_location_countries_france(self) -> None:
        assert gh_parse_countries("Paris, France") == ["France"]
        assert gh_parse_countries("Paris") == ["France"]
        assert gh_parse_countries("Remote - France") == ["France"]
        assert gh_parse_countries("Lyon, France") == ["France"]

    def test_parse_location_countries_germany(self) -> None:
        assert gh_parse_countries("Berlin, Germany") == ["Germany"]
        assert gh_parse_countries("Munich") == ["Germany"]

    def test_parse_location_countries_uk(self) -> None:
        assert gh_parse_countries("London, United Kingdom") == ["United Kingdom"]
        assert gh_parse_countries("London") == ["United Kingdom"]

    def test_parse_location_countries_unknown(self) -> None:
        assert gh_parse_countries("Worldwide") == []
        assert gh_parse_countries("Remote") == []
        assert gh_parse_countries("") == []

    def test_parse_work_mode_remote(self) -> None:
        assert _parse_work_mode("Remote - Europe") == "remote"
        assert _parse_work_mode("Remote") == "remote"

    def test_parse_work_mode_hybrid(self) -> None:
        assert _parse_work_mode("Paris - Hybrid") == "hybrid"

    def test_parse_work_mode_unknown(self) -> None:
        assert _parse_work_mode("Paris, France") is None
        assert _parse_work_mode("") is None


# ---------------------------------------------------------------------------
# LeverCollector — unit tests
# ---------------------------------------------------------------------------

class TestLeverCollector:

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_successful_collection(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            return_value=_mock_response([LEVER_JOB]),
        ):
            result = LeverCollector(config={"sites": ["theodo"]}).collect()

        assert result.source_name == "lever"
        assert len(result.jobs) == 1
        assert result.errors == []
        assert result.listing_complete is True
        job = result.jobs[0]
        assert job["title"] == "AI Architect"
        assert job["company"] == "Theodo"
        assert job["countries"] == ["France"]
        assert job["work_mode"] == "hybrid"
        assert job["canonical_url"] == "https://jobs.lever.co/theodo/abc-def-123"
        assert job["apply_url"] == "https://jobs.lever.co/theodo/abc-def-123/apply"
        assert job["source_job_id"] == "abc-def-123"
        assert job["type"] == "CDI"

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_multiple_sites_iterated(self, _rl: MagicMock) -> None:
        job2 = dict(LEVER_JOB, id="xyz-999", text="Backend Engineer")
        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            side_effect=[_mock_response([LEVER_JOB]), _mock_response([job2])],
        ):
            result = LeverCollector(config={"sites": ["theodo", "qonto"]}).collect()

        assert len(result.jobs) == 2
        assert result.errors == []

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_404_site_skipped_gracefully(self, _rl: MagicMock) -> None:
        err_mock = MagicMock(spec=httpx.Response)
        err_mock.status_code = 404
        http_exc = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=err_mock)

        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            side_effect=http_exc,
        ):
            result = LeverCollector(config={"sites": ["no-such-company"]}).collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "404" in result.errors[0]

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_404_one_site_continues_others(self, _rl: MagicMock) -> None:
        err_mock = MagicMock(spec=httpx.Response)
        err_mock.status_code = 404
        http_exc = httpx.HTTPStatusError("Not Found", request=MagicMock(), response=err_mock)
        ok_resp = _mock_response([LEVER_JOB])

        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            side_effect=[http_exc, ok_resp],
        ):
            result = LeverCollector(config={"sites": ["bad-site", "theodo"]}).collect()

        assert len(result.jobs) == 1
        assert len(result.errors) == 1

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_network_error_adds_error(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            side_effect=RuntimeError("timeout"),
        ):
            result = LeverCollector(config={"sites": ["theodo"]}).collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "timeout" in result.errors[0]
        assert result.listing_complete is False

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_max_jobs_cap(self, _rl: MagicMock) -> None:
        job2 = dict(LEVER_JOB, id="job2", text="DevOps Engineer")
        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            return_value=_mock_response([LEVER_JOB, job2]),
        ):
            result = LeverCollector(config={"sites": ["theodo"], "max_jobs": 1}).collect()

        assert len(result.jobs) == 1

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_empty_site(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            return_value=_mock_response([]),
        ):
            result = LeverCollector(config={"sites": ["theodo"]}).collect()

        assert result.jobs == []
        assert result.errors == []

    def test_no_config_returns_error(self) -> None:
        result = LeverCollector(config={}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1

    def test_board_identifier_merged(self) -> None:
        col = LeverCollector(config={"board_identifier": "alpha", "sites": ["beta"]})
        assert "alpha" in col.sites
        assert "beta" in col.sites

    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_unexpected_format_adds_error(self, _rl: MagicMock) -> None:
        with patch(
            "src.collectors.lever.LeverCollector._fetch_with_retry",
            return_value=_mock_response({"unexpected": "dict"}),
        ):
            result = LeverCollector(config={"sites": ["theodo"]}).collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "unexpected format" in result.errors[0]


# ---------------------------------------------------------------------------
# Lever pure helpers
# ---------------------------------------------------------------------------

class TestLeverPureHelpers:

    def test_map_job_fields(self) -> None:
        out = lv_map_job(LEVER_JOB, "theodo", "Theodo")
        assert out["title"] == "AI Architect"
        assert out["company"] == "Theodo"
        assert out["location"] == "Paris, France"
        assert out["countries"] == ["France"]
        assert out["work_mode"] == "hybrid"
        assert out["remote"] is False
        assert out["source_job_id"] == "abc-def-123"
        assert out["type"] == "CDI"
        assert out["canonical_url"] == "https://jobs.lever.co/theodo/abc-def-123"
        assert out["apply_url"] == "https://jobs.lever.co/theodo/abc-def-123/apply"

    def test_map_job_published_at(self) -> None:
        out = lv_map_job(LEVER_JOB, "theodo", "Theodo")
        assert isinstance(out["published_at"], datetime)
        assert out["published_at"].tzinfo == timezone.utc

    def test_map_job_remote_workplace(self) -> None:
        raw = dict(LEVER_JOB, workplaceType="remote", country="France")
        out = lv_map_job(raw, "theodo", "Theodo")
        assert out["work_mode"] == "remote"
        assert out["remote"] is True

    def test_map_job_on_site_workplace(self) -> None:
        raw = dict(LEVER_JOB, workplaceType="on-site")
        out = lv_map_job(raw, "theodo", "Theodo")
        assert out["work_mode"] == "on_site"
        assert out["remote"] is False

    def test_map_job_country_field_used(self) -> None:
        raw = dict(LEVER_JOB)
        raw["country"] = "Germany"
        raw["categories"] = {**raw["categories"], "location": "Berlin"}
        out = lv_map_job(raw, "contentsquare", "Contentsquare")
        # country field takes precedence over location parse
        assert "Germany" in out["countries"]

    def test_map_job_no_country_falls_back_to_location(self) -> None:
        raw = dict(LEVER_JOB)
        raw["country"] = ""
        raw["categories"] = {**raw["categories"], "location": "London"}
        out = lv_map_job(raw, "somecompany", "Somecompany")
        assert out["countries"] == ["United Kingdom"]

    def test_map_job_description_plain_preferred(self) -> None:
        raw = dict(LEVER_JOB, descriptionPlain="Plain text desc")
        out = lv_map_job(raw, "theodo", "Theodo")
        assert "Plain text desc" in out["description"]

    def test_map_job_lists_appended(self) -> None:
        raw = dict(LEVER_JOB, lists=[{"content": "Requirement 1"}, {"content": "Requirement 2"}])
        out = lv_map_job(raw, "theodo", "Theodo")
        assert "Requirement 1" in out["description"]
        assert "Requirement 2" in out["description"]

    def test_parse_countries_france(self) -> None:
        assert lv_parse_countries("Paris, France") == ["France"]
        assert lv_parse_countries("Remote - France") == ["France"]

    def test_parse_countries_unknown(self) -> None:
        assert lv_parse_countries("Worldwide") == []
        assert lv_parse_countries("") == []

    def test_company_name_derived_from_site(self) -> None:
        out = lv_map_job(LEVER_JOB, "my-startup", "My Startup")
        assert out["company"] == "My Startup"
