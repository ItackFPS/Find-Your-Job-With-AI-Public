"""Tests for the second collector expansion round: Bundesagentur (Germany),
Arbetsformedlingen (Sweden), Lever, and SmartRecruiters.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.arbetsformedlingen import ArbetsformedlingenCollector
from src.collectors.bundesagentur import BundesagenturCollector
from src.collectors.smartrecruiters import SmartRecruitersCollector

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


# ---------------------------------------------------------------------------
# Sample raw payloads
# ---------------------------------------------------------------------------

SAMPLE_BUNDESAGENTUR_JOB: dict[str, Any] = {
    "referenznummer": "10000-1234567-S",
    "stellenangebotsTitel": "Softwareentwickler (m/w/d)",
    "firma": "TechFirma GmbH",
    "stellenlokationen": [{"adresse": {"ort": "Berlin", "region": "Berlin"}}],
    "homeofficemoeglich": True,
    "arbeitszeitVollzeit": True,
    "externeURL": "https://example.com/job/1",
    "datumErsteVeroeffentlichung": "2026-08-01",
}

SAMPLE_ARBETSFORMEDLINGEN_JOB: dict[str, Any] = {
    "id": "abc123",
    "headline": "Mjukvaruutvecklare",
    "employer": {"name": "SwedeTech AB"},
    "description": {"text": "Utveckla mjukvara"},
    "workplace_address": {"municipality": "Stockholm", "region": "Stockholm"},
    "workplace_model": {"label": "Hybrid"},
    "working_hours_type": {"label": "Heltid"},
    "webpage_url": "https://arbetsformedlingen.se/jobs/abc123",
    "publication_date": "2026-08-01",
}

SAMPLE_SMARTRECRUITERS_JOB: dict[str, Any] = {
    "id": "sr-1",
    "name": "Data Analyst",
    "company": {"name": "BoschGroup"},
    "location": {"city": "Paris", "fullLocation": "Paris, France", "remote": False, "country": "fr"},
    "typeOfEmployment": {"label": "Full-time"},
    "releasedDate": "2026-08-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Bundesagentur (Germany)
# ---------------------------------------------------------------------------

class TestBundesagenturCollector:
    @patch("src.collectors.bundesagentur.BundesagenturCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.bundesagentur.BundesagenturCollector._fetch_with_retry",
                   return_value=_mock_response({"ergebnisliste": [SAMPLE_BUNDESAGENTUR_JOB]})):
            result = BundesagenturCollector(config={"search_terms": ["Softwareentwickler"]}).collect()
        assert result.source_name == "bundesagentur"
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Softwareentwickler (m/w/d)"
        assert job["company"] == "TechFirma GmbH"
        assert job["countries"] == ["Germany"]
        assert job["remote"] is True
        assert job["employment_type"] == "full-time"
        # homeofficemoeglich=True is recorded as "hybrid" (not "remote") in
        # work_mode — more precise than the legacy boolean, which is
        # unchanged for backward compatibility.
        assert job["work_mode"] == "hybrid"

    def test_work_mode_unknown_when_homeoffice_not_possible(self) -> None:
        raw = dict(SAMPLE_BUNDESAGENTUR_JOB, homeofficemoeglich=False)
        out = BundesagenturCollector._normalize_fields(raw)
        assert out["remote"] is False
        assert out["work_mode"] is None

    @patch("src.collectors.bundesagentur.BundesagenturCollector._enforce_rate_limit")
    def test_empty_response(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.bundesagentur.BundesagenturCollector._fetch_with_retry",
                   return_value=_mock_response({"ergebnisliste": []})):
            result = BundesagenturCollector(config={"search_terms": ["IT"]}).collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.bundesagentur.BundesagenturCollector._enforce_rate_limit")
    def test_network_error_per_term(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.bundesagentur.BundesagenturCollector._fetch_with_retry",
                   side_effect=RuntimeError("timeout")):
            result = BundesagenturCollector(config={"search_terms": ["IT"]}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "IT" in result.errors[0]

    @patch("src.collectors.bundesagentur.BundesagenturCollector._enforce_rate_limit")
    def test_deduplication_across_search_terms(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.bundesagentur.BundesagenturCollector._fetch_with_retry",
                   return_value=_mock_response({"ergebnisliste": [SAMPLE_BUNDESAGENTUR_JOB]})):
            result = BundesagenturCollector(config={"search_terms": ["IT", "Softwareentwickler"]}).collect()
        assert len(result.jobs) == 1

    def test_normalize_fields_missing_optional(self) -> None:
        out = BundesagenturCollector._normalize_fields({"stellenangebotsTitel": "X"})
        assert out["title"] == "X"
        assert out["countries"] == ["Germany"]
        assert out["location"] == "Germany"
        assert "employment_type" not in out


# ---------------------------------------------------------------------------
# Arbetsformedlingen (Sweden)
# ---------------------------------------------------------------------------

class TestArbetsformedlingenCollector:
    @patch("src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._fetch_with_retry",
            return_value=_mock_response({"hits": [SAMPLE_ARBETSFORMEDLINGEN_JOB]}),
        ):
            result = ArbetsformedlingenCollector(config={"search_terms": ["developer"]}).collect()
        assert result.source_name == "arbetsformedlingen"
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Mjukvaruutvecklare"
        assert job["company"] == "SwedeTech AB"
        assert job["countries"] == ["Sweden"]
        assert job["remote"] is True
        assert job["employment_type"] == "full-time"

    @patch("src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._enforce_rate_limit")
    def test_empty_response(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._fetch_with_retry",
            return_value=_mock_response({"hits": []}),
        ):
            result = ArbetsformedlingenCollector(config={"search_terms": ["developer"]}).collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._enforce_rate_limit")
    def test_network_error_per_term(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._fetch_with_retry",
            side_effect=RuntimeError("timeout"),
        ):
            result = ArbetsformedlingenCollector(config={"search_terms": ["developer"]}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1

    @patch("src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._enforce_rate_limit")
    def test_deduplication_across_search_terms(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._fetch_with_retry",
            return_value=_mock_response({"hits": [SAMPLE_ARBETSFORMEDLINGEN_JOB]}),
        ):
            result = ArbetsformedlingenCollector(
                config={"search_terms": ["developer", "software engineer"]}
            ).collect()
        assert len(result.jobs) == 1

    def test_normalize_fields_missing_optional(self) -> None:
        out = ArbetsformedlingenCollector._normalize_fields({"headline": "X"})
        assert out["title"] == "X"
        assert out["countries"] == ["Sweden"]
        assert out["company"] == ""
        assert out["remote"] is False


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------

class TestSmartRecruitersCollector:
    def test_missing_board_identifier_skips_collection(self) -> None:
        result = SmartRecruitersCollector().collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "board_identifier" in result.errors[0]

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_successful_collection(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = _mock_response(
            {"content": [SAMPLE_SMARTRECRUITERS_JOB], "totalFound": 1}
        )
        result = SmartRecruitersCollector(config={"board_identifier": "BoschGroup"}).collect()
        assert result.source_name == "smartrecruiters"
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Data Analyst"
        assert job["company"] == "BoschGroup"
        assert job["countries"] == ["France"]
        assert job["remote"] is False
        assert job["employment_type"] == "full-time"
        assert job["url"] == "https://jobs.smartrecruiters.com/BoschGroup/sr-1"

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_pagination(self, mock_fetch: MagicMock) -> None:
        page1_jobs = [dict(SAMPLE_SMARTRECRUITERS_JOB, id=f"sr-{i}") for i in range(100)]
        page2_jobs = [dict(SAMPLE_SMARTRECRUITERS_JOB, id="sr-100")]
        mock_fetch.side_effect = [
            _mock_response({"content": page1_jobs, "totalFound": 101}),
            _mock_response({"content": page2_jobs, "totalFound": 101}),
        ]
        result = SmartRecruitersCollector(config={"board_identifier": "BoschGroup"}).collect()
        assert len(result.jobs) == 101
        assert mock_fetch.call_count == 2

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_empty_response(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = _mock_response({"content": [], "totalFound": 0})
        result = SmartRecruitersCollector(config={"board_identifier": "BoschGroup"}).collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_malformed_response(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = _mock_response({"unexpected": "shape"})
        result = SmartRecruitersCollector(config={"board_identifier": "BoschGroup"}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_network_error(self, mock_fetch: MagicMock) -> None:
        mock_fetch.side_effect = RuntimeError("timeout")
        result = SmartRecruitersCollector(config={"board_identifier": "BoschGroup"}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1

    def test_normalize_fields_missing_optional(self) -> None:
        out = SmartRecruitersCollector._normalize_fields({"name": "X"}, "BoschGroup")
        assert out["title"] == "X"
        assert out["company"] == "BoschGroup"
        assert out["countries"] == []
        assert out["remote"] is False

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_countries_config_issues_one_request_per_country_with_filter(
        self, mock_fetch: MagicMock
    ) -> None:
        """When 'countries' is configured, one paginated request sequence is
        issued per country, each passing the API's single-value 'country'
        filter (verified live: combining multiple country values in one
        request returns zero results)."""
        mock_fetch.return_value = _mock_response({"content": [SAMPLE_SMARTRECRUITERS_JOB], "totalFound": 1})
        result = SmartRecruitersCollector(
            config={"board_identifier": "BoschGroup", "countries": ["France", "Germany"]}
        ).collect()

        assert mock_fetch.call_count == 2
        called_country_params = [call.kwargs["params"]["country"] for call in mock_fetch.call_args_list]
        assert called_country_params == ["fr", "de"]
        assert len(result.jobs) == 2

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_unknown_country_name_is_skipped_without_crashing(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = _mock_response({"content": [], "totalFound": 0})
        result = SmartRecruitersCollector(
            config={"board_identifier": "BoschGroup", "countries": ["Atlantis", "France"]}
        ).collect()

        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args.kwargs["params"]["country"] == "fr"
        assert result.errors == []

    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._fetch_with_retry")
    def test_no_countries_config_makes_a_single_unfiltered_request(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = _mock_response({"content": [SAMPLE_SMARTRECRUITERS_JOB], "totalFound": 1})
        SmartRecruitersCollector(config={"board_identifier": "BoschGroup"}).collect()

        mock_fetch.assert_called_once()
        assert "country" not in mock_fetch.call_args.kwargs["params"]
