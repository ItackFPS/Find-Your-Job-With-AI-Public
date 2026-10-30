"""Tests for the France Travail collector.

Covers:
  - Parser: field mapping from France Travail API response to the generic dict
  - Error handling: missing credentials, HTTP errors, malformed responses
  - Integration: FranceTravailCollector → normalize_jobs → validate_jobs →
                 deduplicate_jobs (full pipeline with mocked HTTP)

No live API credentials are required. All HTTP calls are mocked.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Module-level fixture: suppress rate-limiter sleeps for all tests in this file.
# The rate limiter calls time.sleep() between API requests; mocked HTTP is
# instantaneous so we don't need actual pauses.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)

from src.collectors.france_travail import (
    FranceTravailCollector,
    _CONTRACT_TYPE_MAP,
    _DEFAULT_SEARCH_KEYWORDS,
    _JOB_URL_TEMPLATE,
    _parse_content_range_total,
)
from src.collectors.base import CollectorResult
from src.pipeline.deduplicate import deduplicate_jobs
from src.pipeline.normalize import normalize_jobs
from src.pipeline.validate import validate_jobs


# ---------------------------------------------------------------------------
# Representative API fixtures
# ---------------------------------------------------------------------------

_SAMPLE_TOKEN_RESPONSE: dict[str, Any] = {
    "access_token": "test-bearer-token-abc123",
    "token_type": "Bearer",
    "expires_in": 1440,
    "scope": "o2dsoffre api_offresdemploiv2",
}

_SAMPLE_JOB_RESPONSE: dict[str, Any] = {
    "resultats": [
        {
            "id": "169BMDS",
            "intitule": "Développeur Python Senior",
            "description": (
                "Nous recherchons un développeur Python expérimenté pour rejoindre "
                "notre équipe. Missions : conception, développement, code review."
            ),
            "dateCreation": "2026-08-20T14:22:00.000Z",
            "dateActualisation": "2026-08-22T10:15:00.000Z",
            "lieuTravail": {
                "libelle": "75 - Paris",
                "latitude": 48.8566,
                "longitude": 2.3522,
                "codePostal": "75001",
                "commune": "75001",
            },
            "entreprise": {
                "nom": "Tech Startup SAS",
                "description": "Startup innovante dans la fintech.",
            },
            "typeContrat": "CDI",
            "typeContratLibelle": "Contrat à durée indéterminée",
            "alternance": False,
            "competences": [
                {"code": "P101", "libelle": "Python"},
                {"code": "P102", "libelle": "SQL"},
            ],
            "salaire": {"libelle": "Annuel de 45000 à 60000 Euros"},
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        },
        {
            "id": "ABC123",
            "intitule": "Data Analyst",
            "description": "Poste en télétravail complet. Analyse de données.",
            "dateCreation": "2026-08-19T09:00:00.000Z",
            "lieuTravail": {"libelle": "69 - Lyon"},
            "entreprise": {"nom": "Data Corp"},
            "typeContrat": "CDD",
            "alternance": False,
            "competences": [],
            "origineOffre": {
                "origine": "2",
                "urlOrigine": "https://datacorp.fr/careers/data-analyst",
                "partenaires": [],
            },
        },
        {
            "id": "ALT999",
            "intitule": "Alternant Développeur Web",
            "description": "Contrat d'alternance - développement web.",
            "dateCreation": "2026-08-18T08:00:00.000Z",
            "lieuTravail": {"libelle": "33 - Bordeaux"},
            "entreprise": {"nom": "Web Agency"},
            "typeContrat": "CDD",   # alternance overrides this
            "alternance": True,
            "competences": [{"code": "W001", "libelle": "JavaScript"}],
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        },
    ]
}


def _mock_httpx_response(
    json_data: dict[str, Any],
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.text = str(json_data)
    resp.raise_for_status.return_value = None
    # Support dict-style header access via .get()
    _headers: dict[str, str] = headers or {}
    resp.headers = MagicMock()
    resp.headers.get = lambda key, default="": _headers.get(key, default)
    return resp


def _mock_httpx_error_response(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    """Mock an HTTP error response (non-2xx)."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = False
    resp.text = f"HTTP {status_code} error"
    _headers: dict[str, str] = headers or {}
    resp.headers = MagicMock()
    resp.headers.get = lambda key, default="": _headers.get(key, default)
    return resp


# ---------------------------------------------------------------------------
# Tests: field mapping (_normalize_fields)
# ---------------------------------------------------------------------------


class TestFranceTravailNormalizeFields:
    """Unit tests for the static field translator."""

    def test_job_id_is_mapped_to_source_job_id(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["source_job_id"] == "169BMDS"

    def test_title_mapped_from_intitule(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["title"] == "Développeur Python Senior"

    def test_company_mapped_from_entreprise_nom(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["company"] == "Tech Startup SAS"

    def test_location_mapped_from_lieu_travail_libelle(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["location"] == "75 - Paris"

    def test_countries_always_contains_france(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["countries"] == ["France"]

    def test_department_code_extracted_from_libelle_prefix(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]  # "75 - Paris"
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["department_code"] == "75"

    def test_department_code_extracted_for_other_departments(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][1]  # "69 - Lyon"
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["department_code"] == "69"

    def test_department_code_falls_back_to_postal_code(self) -> None:
        raw = {
            "id": "X1",
            "intitule": "Test",
            "lieuTravail": {"libelle": "Paris", "codePostal": "75008"},
            "entreprise": {"nom": "Test Co"},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["department_code"] == "75"

    def test_department_code_is_none_when_undeterminable(self) -> None:
        raw = {
            "id": "X2",
            "intitule": "Test",
            "lieuTravail": {"libelle": "Télétravail"},
            "entreprise": {"nom": "Test Co"},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["department_code"] is None

    def test_department_code_handles_corsica(self) -> None:
        raw = {
            "id": "X3",
            "intitule": "Test",
            "lieuTravail": {"libelle": "2A - Ajaccio"},
            "entreprise": {"nom": "Test Co"},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["department_code"] == "2A"

    def test_url_falls_back_to_francetravail_portal_when_no_origin_url(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["url"] == _JOB_URL_TEMPLATE.format("169BMDS")
        assert "169BMDS" in result["url"]

    def test_url_uses_origin_url_when_available(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][1]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["url"] == "https://datacorp.fr/careers/data-analyst"

    def test_cdi_maps_to_full_time(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["type"] == "full-time"

    def test_cdd_maps_to_contract(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][1]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["type"] == "contract"

    def test_alternance_maps_to_internship_regardless_of_type_contrat(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][2]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["type"] == "internship"

    def test_non_remote_job_has_remote_false(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["remote"] is False

    def test_teletravail_in_description_sets_remote_true(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][1]  # "Poste en télétravail complet"
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["remote"] is True

    def test_teletravail_in_location_sets_remote_true(self) -> None:
        raw = {
            "id": "TELE1",
            "intitule": "Dev Remote",
            "description": "Description normale.",
            "lieuTravail": {"libelle": "Télétravail"},
            "entreprise": {"nom": "Remote Co"},
            "typeContrat": "CDI",
            "alternance": False,
            "competences": [],
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["remote"] is True

    def test_france_entiere_location_sets_remote_true(self) -> None:
        raw = {
            "id": "NAT1",
            "intitule": "Dev National",
            "description": "Poste en France.",
            "lieuTravail": {"libelle": "FRANCE ENTIÈRE"},
            "entreprise": {"nom": "Big Corp"},
            "typeContrat": "CDI",
            "alternance": False,
            "competences": [],
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["remote"] is True

    def test_skills_extracted_from_competences(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert "Python" in result["skills"]
        assert "SQL" in result["skills"]

    def test_empty_competences_gives_empty_skills(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][1]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["skills"] == []

    def test_published_at_mapped_from_date_creation(self) -> None:
        raw = _SAMPLE_JOB_RESPONSE["resultats"][0]
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["published_at"] == "2026-08-20T14:22:00.000Z"

    def test_missing_entreprise_gives_empty_company(self) -> None:
        raw = {
            "id": "NOENT",
            "intitule": "Mystery Role",
            "description": "Unknown company.",
            "lieuTravail": {"libelle": "Paris"},
            "typeContrat": "CDI",
            "alternance": False,
            "competences": [],
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["company"] == ""

    def test_unknown_contract_type_gives_none(self) -> None:
        raw = {
            "id": "UNK1",
            "intitule": "Some Role",
            "description": ".",
            "lieuTravail": {"libelle": "Nantes"},
            "entreprise": {"nom": "Corp"},
            "typeContrat": "UNKNOWN_CODE",
            "alternance": False,
            "competences": [],
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        }
        result = FranceTravailCollector._normalize_fields(raw)
        assert result["type"] is None


# ---------------------------------------------------------------------------
# Tests: missing credentials
# ---------------------------------------------------------------------------


class TestFranceTravailMissingCredentials:
    def test_missing_both_credentials_returns_no_jobs_with_error(self) -> None:
        # Instantiate inside the patch so the constructor sees no credentials
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRANCE_TRAVAIL_CLIENT_ID", None)
            os.environ.pop("FRANCE_TRAVAIL_CLIENT_SECRET", None)
            collector = FranceTravailCollector(config={})
            result = collector.collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "FRANCE_TRAVAIL_CLIENT_ID" in result.errors[0]
        assert "francetravail.fr" in result.errors[0]

    def test_missing_credentials_makes_no_http_call(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRANCE_TRAVAIL_CLIENT_ID", None)
            os.environ.pop("FRANCE_TRAVAIL_CLIENT_SECRET", None)
            with patch.object(httpx.Client, "post") as mock_post, patch.object(httpx.Client, "get") as mock_get:
                collector = FranceTravailCollector(config={})
                collector.collect()

        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_credentials_from_env_vars_are_used(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FRANCE_TRAVAIL_CLIENT_ID": "env-id",
                "FRANCE_TRAVAIL_CLIENT_SECRET": "env-secret",
            },
        ):
            collector = FranceTravailCollector(config={})
            assert collector.client_id == "env-id"
            assert collector.client_secret == "env-secret"

    def test_credentials_from_config_override_env_vars(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FRANCE_TRAVAIL_CLIENT_ID": "env-id",
                "FRANCE_TRAVAIL_CLIENT_SECRET": "env-secret",
            },
        ):
            collector = FranceTravailCollector(
                config={"client_id": "cfg-id", "client_secret": "cfg-secret"}
            )
            assert collector.client_id == "cfg-id"
            assert collector.client_secret == "cfg-secret"


# ---------------------------------------------------------------------------
# Tests: HTTP / API error handling
# ---------------------------------------------------------------------------


class TestFranceTravailErrorHandling:
    def _make_collector(self) -> FranceTravailCollector:
        return FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"}
        )

    def test_token_http_error_is_captured_not_raised(self) -> None:
        collector = self._make_collector()
        error_resp = _mock_httpx_error_response(401)

        with patch("httpx.post", return_value=error_resp):
            result = collector.collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "failed" in result.errors[0].lower()

    def test_search_http_error_is_captured_not_raised(self) -> None:
        collector = self._make_collector()
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        error_resp = _mock_httpx_error_response(500)

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=error_resp),
        ):
            result = collector.collect()

        assert result.jobs == []
        assert len(result.errors) == 1

    def test_malformed_api_response_missing_resultats_captured(self) -> None:
        collector = self._make_collector()
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        bad_resp = _mock_httpx_response({"unexpected_key": []})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=bad_resp),
        ):
            result = collector.collect()

        # "resultats" key is missing → empty list, no crash
        assert result.jobs == []
        assert result.errors == []

    def test_resultats_not_a_list_captured_as_error(self) -> None:
        collector = self._make_collector()
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        bad_resp = _mock_httpx_response({"resultats": "not-a-list"})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=bad_resp),
        ):
            result = collector.collect()

        assert result.jobs == []
        assert len(result.errors) == 1

    def test_non_dict_job_entry_is_skipped_with_error_entry(self) -> None:
        collector = self._make_collector()
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        mixed_resp = _mock_httpx_response(
            {"resultats": ["not-a-dict", _SAMPLE_JOB_RESPONSE["resultats"][0]]}
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=mixed_resp),
        ):
            result = collector.collect()

        # One valid job, one error
        assert len(result.jobs) == 1
        assert len(result.errors) == 1

    def test_network_error_on_token_request_is_captured(self) -> None:
        collector = self._make_collector()

        with patch("httpx.post", side_effect=httpx.ConnectError("no route")):
            result = collector.collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "failed" in result.errors[0].lower()


# ---------------------------------------------------------------------------
# Tests: successful collection (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFranceTravailCollect:
    def _make_collector(self, max_jobs: int = 20) -> FranceTravailCollector:
        return FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"},
            max_jobs=max_jobs,
        )

    def _mock_successful_call(
        self, collector: FranceTravailCollector
    ) -> CollectorResult:
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response(_SAMPLE_JOB_RESPONSE)

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp),
        ):
            return collector.collect()

    def test_successful_collect_returns_expected_job_count(self) -> None:
        collector = self._make_collector()
        result = self._mock_successful_call(collector)
        assert len(result.jobs) == 3
        assert result.errors == []

    def test_source_name_is_france_travail(self) -> None:
        collector = self._make_collector()
        result = self._mock_successful_call(collector)
        assert result.source_name == "france_travail"

    def test_job_fields_are_populated(self) -> None:
        collector = self._make_collector()
        result = self._mock_successful_call(collector)
        job = result.jobs[0]
        assert job["title"] == "Développeur Python Senior"
        assert job["company"] == "Tech Startup SAS"
        assert job["location"] == "75 - Paris"
        assert job["countries"] == ["France"]
        assert job["remote"] is False
        assert job["type"] == "full-time"
        assert job["source_job_id"] == "169BMDS"

    def test_max_jobs_is_forwarded_as_range_param(self) -> None:
        collector = self._make_collector(max_jobs=5)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response({"resultats": []})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp) as mock_get,
        ):
            collector.collect()

        params = mock_get.call_args.kwargs.get("params", {})
        # max_jobs=5, page_size=150 → batch_size=5 → range=0-4
        assert params.get("range") == "0-4"

    def test_bearer_token_sent_in_authorization_header(self) -> None:
        collector = self._make_collector()
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response({"resultats": []})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp) as mock_get,
        ):
            collector.collect()

        headers = mock_get.call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer test-bearer-token-abc123"

    def test_config_max_jobs_overrides_default(self) -> None:
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "s", "max_jobs": 5}
        )
        assert collector.max_jobs == 5

    def test_default_max_jobs_is_none_unlimited(self) -> None:
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "s"}
        )
        assert collector.max_jobs is None


# ---------------------------------------------------------------------------
# Tests: pipeline integration (collect → normalize → validate → deduplicate)
# ---------------------------------------------------------------------------


class TestFranceTravailPipelineIntegration:
    def _run_pipeline(self) -> tuple[list, list, list]:
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"}
        )
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response(_SAMPLE_JOB_RESPONSE)

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp),
        ):
            result = collector.collect()

        raw_jobs = result.jobs
        normalized = normalize_jobs("france_travail", raw_jobs)
        valid_jobs, errors = validate_jobs(normalized)
        deduped, removed = deduplicate_jobs(valid_jobs)
        return normalized, valid_jobs, deduped

    def test_normalized_jobs_have_correct_source(self) -> None:
        normalized, _, _ = self._run_pipeline()
        for job in normalized:
            assert job.source == "france_travail"

    def test_normalized_jobs_have_countries_france(self) -> None:
        normalized, _, _ = self._run_pipeline()
        for job in normalized:
            assert "France" in job.countries

    def test_all_jobs_pass_validation(self) -> None:
        normalized, valid_jobs, _ = self._run_pipeline()
        assert len(valid_jobs) == len(normalized)

    def test_no_duplicates_in_sample_data(self) -> None:
        _, valid_jobs, deduped = self._run_pipeline()
        assert len(deduped) == len(valid_jobs)

    def test_remote_detection_survives_normalization(self) -> None:
        normalized, _, _ = self._run_pipeline()
        # resultats[1] has "télétravail" in description → remote=True
        data_analyst = next(
            j for j in normalized if j.title == "Data Analyst"
        )
        assert data_analyst.remote is True

    def test_python_dev_job_is_not_remote(self) -> None:
        normalized, _, _ = self._run_pipeline()
        py_dev = next(
            j for j in normalized if "Python Senior" in (j.title or "")
        )
        assert py_dev.remote is False

    def test_duplicate_entry_is_removed(self) -> None:
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"}
        )
        # Same job twice — the collector now deduplicates by source_job_id
        # within _search_all_queries, so the duplicate never reaches the
        # pipeline deduplication stage.
        dup_response = {
            "resultats": [
                _SAMPLE_JOB_RESPONSE["resultats"][0],
                _SAMPLE_JOB_RESPONSE["resultats"][0],  # exact duplicate
            ]
        }
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response(dup_response)

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp),
        ):
            result = collector.collect()

        normalized = normalize_jobs("france_travail", result.jobs)
        valid_jobs, _ = validate_jobs(normalized)
        deduped, _ = deduplicate_jobs(valid_jobs)

        # Only 1 unique job should survive regardless of which stage removed it
        assert len(deduped) == 1


# ---------------------------------------------------------------------------
# Tests: contract type mapping completeness
# ---------------------------------------------------------------------------


class TestContractTypeMap:
    def test_all_mapped_values_are_valid_normalizer_types(self) -> None:
        valid_types = {"full-time", "part-time", "contract", "internship"}
        for code, mapped in _CONTRACT_TYPE_MAP.items():
            assert mapped in valid_types, (
                f"Contract code {code!r} maps to {mapped!r}, "
                f"which is not in the normalizer's valid set"
            )


# ---------------------------------------------------------------------------
# Tests: Content-Range header parsing
# ---------------------------------------------------------------------------


class TestParseContentRangeTotal:
    def test_parses_standard_format(self) -> None:
        assert _parse_content_range_total("offres 0-149/5000") == 5000

    def test_parses_small_result_set(self) -> None:
        assert _parse_content_range_total("offres 0-19/47") == 47

    def test_returns_none_for_empty_string(self) -> None:
        assert _parse_content_range_total("") is None

    def test_returns_none_for_wildcard_total(self) -> None:
        assert _parse_content_range_total("offres 0-149/*") is None

    def test_returns_none_for_malformed_header(self) -> None:
        assert _parse_content_range_total("not-a-range-header") is None


# ---------------------------------------------------------------------------
# Tests: keyword search parameter
# ---------------------------------------------------------------------------


class TestFranceTravailKeywords:
    def _make_collector(self, config: dict | None = None) -> FranceTravailCollector:
        cfg = {"client_id": "id", "client_secret": "secret"}
        if config:
            cfg.update(config)
        return FranceTravailCollector(config=cfg)

    def test_default_keywords_sent_in_motsCles(self) -> None:
        collector = self._make_collector()
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response({"resultats": []})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp) as mock_get,
        ):
            collector.collect()

        params = mock_get.call_args.kwargs.get("params", {})
        assert params.get("motsCles") == _DEFAULT_SEARCH_KEYWORDS

    def test_custom_keywords_from_config_override_default(self) -> None:
        collector = self._make_collector(config={"keywords": "développeur logiciel"})
        assert collector.search_keywords == "développeur logiciel"

        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response({"resultats": []})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp) as mock_get,
        ):
            collector.collect()

        params = mock_get.call_args.kwargs.get("params", {})
        assert params.get("motsCles") == "développeur logiciel"

    def test_default_max_jobs_is_none_exhaustive(self) -> None:
        collector = self._make_collector()
        assert collector.max_jobs is None


# ---------------------------------------------------------------------------
# Tests: pagination
# ---------------------------------------------------------------------------


def _make_jobs(n: int, id_prefix: str = "JOB") -> list[dict[str, Any]]:
    """Generate n minimal France Travail job entries with distinct IDs."""
    return [
        {
            "id": f"{id_prefix}{i:04d}",
            "intitule": f"Développeur {i}",
            "description": "Description courte.",
            "lieuTravail": {"libelle": "75 - Paris"},
            "entreprise": {"nom": "Corp"},
            "typeContrat": "CDI",
            "alternance": False,
            "competences": [],
            "origineOffre": {"origine": "1", "urlOrigine": None, "partenaires": []},
        }
        for i in range(n)
    ]


class TestFranceTravailPagination:
    """Verify the collector paginates correctly using mocked multi-page responses."""

    def _make_collector(self, max_jobs: int = 150, page_size: int = 20) -> FranceTravailCollector:
        return FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"},
            max_jobs=max_jobs,
            _page_size=page_size,
        )

    def test_three_pages_returns_47_jobs_not_20(self) -> None:
        """page 1=20, page 2=20, page 3=7 → collector returns 47, not just 20."""
        collector = self._make_collector(max_jobs=100, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(20, "P1")},
            headers={"Content-Range": "offres 0-19/47"},
        )
        page2 = _mock_httpx_response(
            {"resultats": _make_jobs(20, "P2")},
            headers={"Content-Range": "offres 20-39/47"},
        )
        page3 = _mock_httpx_response(
            {"resultats": _make_jobs(7, "P3")},
            headers={"Content-Range": "offres 40-46/47"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[page1, page2, page3]) as mock_get,
        ):
            result = collector.collect()

        assert len(result.jobs) == 47
        assert mock_get.call_count == 3

    def test_collector_requests_subsequent_pages(self) -> None:
        """Verify the range parameter advances correctly across pages."""
        collector = self._make_collector(max_jobs=60, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        full_page = _mock_httpx_response(
            {"resultats": _make_jobs(20)},
            headers={"Content-Range": "offres 0-19/200"},
        )
        full_page2 = _mock_httpx_response(
            {"resultats": _make_jobs(20, "B")},
            headers={"Content-Range": "offres 20-39/200"},
        )
        partial_page = _mock_httpx_response(
            {"resultats": _make_jobs(10, "C")},
            headers={"Content-Range": "offres 40-49/200"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[full_page, full_page2, partial_page]) as mock_get,
        ):
            # max_jobs=60 but only 50 available (last page partial) → collect 50
            result = collector.collect()

        ranges = [
            call.kwargs["params"]["range"]
            for call in mock_get.call_args_list
        ]
        assert ranges[0] == "0-19"
        assert ranges[1] == "20-39"
        assert ranges[2] == "40-59"
        assert len(result.jobs) == 50

    def test_max_jobs_caps_collection(self) -> None:
        """Collector stops after max_jobs even if more pages exist."""
        collector = self._make_collector(max_jobs=25, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(20)},
            headers={"Content-Range": "offres 0-19/1000"},
        )
        page2 = _mock_httpx_response(
            {"resultats": _make_jobs(20, "B")},
            headers={"Content-Range": "offres 20-39/1000"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[page1, page2]) as mock_get,
        ):
            result = collector.collect()

        assert len(result.jobs) == 25
        assert mock_get.call_count == 2

    def test_empty_page_terminates_pagination(self) -> None:
        """An empty resultats response stops the loop immediately."""
        collector = self._make_collector(max_jobs=100, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(20)},
            headers={"Content-Range": "offres 0-19/20"},
        )
        empty_page = _mock_httpx_response(
            {"resultats": []},
            headers={"Content-Range": "offres 20-39/20"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[page1, empty_page]),
        ):
            result = collector.collect()

        # Stopped after page1 because Content-Range total (20) was reached
        # OR because empty_page was received — both are valid termination signals
        assert len(result.jobs) == 20

    def test_api_total_from_content_range_stops_pagination(self) -> None:
        """When collected == api_total, no further requests are made."""
        collector = self._make_collector(max_jobs=100, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(20)},
            headers={"Content-Range": "offres 0-19/20"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=page1) as mock_get,
        ):
            result = collector.collect()

        assert len(result.jobs) == 20
        assert mock_get.call_count == 1

    def test_no_duplicates_across_pages(self) -> None:
        """Jobs from different pages must have distinct source_job_ids."""
        collector = self._make_collector(max_jobs=40, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(20, "PA")},
            headers={"Content-Range": "offres 0-19/40"},
        )
        page2 = _mock_httpx_response(
            {"resultats": _make_jobs(20, "PB")},
            headers={"Content-Range": "offres 20-39/40"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[page1, page2]),
        ):
            result = collector.collect()

        ids = [j["source_job_id"] for j in result.jobs]
        assert len(ids) == len(set(ids))  # no duplicates

    def test_http_error_on_second_page_is_captured(self) -> None:
        """A permanent HTTP error on page 2 stops pagination; partial results are preserved."""
        collector = self._make_collector(max_jobs=40, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(20)},
            headers={"Content-Range": "offres 0-19/100"},
        )
        error_resp = _mock_httpx_error_response(400)

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[page1, error_resp]),
        ):
            result = collector.collect()

        # Page 1 jobs are preserved; error from page 2 is captured
        assert len(result.errors) >= 1
        assert "failed" in result.errors[0].lower() or "400" in result.errors[0]
        assert result.listing_complete is False

    def test_missing_credentials_makes_no_http_call_pagination(self) -> None:
        """Credentials check happens before any pagination attempt."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRANCE_TRAVAIL_CLIENT_ID", None)
            os.environ.pop("FRANCE_TRAVAIL_CLIENT_SECRET", None)
            with patch.object(httpx.Client, "get") as mock_get:
                collector = FranceTravailCollector(config={})
                collector.collect()
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: discovery_queries — multi-query collection
# ---------------------------------------------------------------------------


class TestFranceTravailMultiQuery:
    """Verify multi-query discovery behavior introduced in the query expansion layer."""

    def _make_collector(
        self, queries: list[str], max_jobs: int | None = None, page_size: int = 20
    ) -> FranceTravailCollector:
        cfg: dict[str, Any] = {
            "client_id": "id",
            "client_secret": "secret",
            "discovery_queries": queries,
            "max_workers": 1,  # sequential so side_effect order is deterministic
        }
        if max_jobs is not None:
            cfg["max_jobs"] = max_jobs
        return FranceTravailCollector(config=cfg, _page_size=page_size)

    def test_discovery_queries_override_search_keywords(self) -> None:
        collector = FranceTravailCollector(
            config={
                "client_id": "id",
                "client_secret": "secret",
                "keywords": "old-keyword",
                "discovery_queries": ["développement", "logiciel"],
            }
        )
        assert collector.discovery_queries == ["développement", "logiciel"]

    def test_single_keyword_config_still_works_as_before(self) -> None:
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret", "keywords": "custom-kw"}
        )
        assert collector.discovery_queries == ["custom-kw"]

    def test_default_discovery_queries_is_informatique(self) -> None:
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"}
        )
        assert collector.discovery_queries == [_DEFAULT_SEARCH_KEYWORDS]

    def test_multiple_queries_each_run_to_exhaustion(self) -> None:
        """Each query is fully paginated; no artificial per-query cap."""
        # No max_jobs → unlimited; each query runs until Content-Range exhausted
        collector = self._make_collector(["informatique", "développement"], page_size=10)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_q1 = _mock_httpx_response(
            {"resultats": _make_jobs(5, "Q1")},
            headers={"Content-Range": "offres 0-4/5"},
        )
        jobs_q2 = _mock_httpx_response(
            {"resultats": _make_jobs(5, "Q2")},
            headers={"Content-Range": "offres 0-4/5"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[jobs_q1, jobs_q2]) as mock_get,
        ):
            result = collector.collect()

        # Both queries run: two API calls, all unique jobs collected
        assert mock_get.call_count == 2
        assert len(result.jobs) == 10
        assert result.listing_complete is True

    def test_duplicate_jobs_across_queries_are_removed(self) -> None:
        """A job returned by two different queries must appear only once."""
        collector = self._make_collector(["informatique", "logiciel"], max_jobs=50, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)

        shared_job = _make_jobs(1, "SHARED")[0]   # same id in both queries
        q1_jobs = [shared_job] + _make_jobs(2, "ONLY_Q1")
        q2_jobs = [shared_job] + _make_jobs(2, "ONLY_Q2")

        resp_q1 = _mock_httpx_response(
            {"resultats": q1_jobs},
            headers={"Content-Range": f"offres 0-{len(q1_jobs)-1}/{len(q1_jobs)}"},
        )
        resp_q2 = _mock_httpx_response(
            {"resultats": q2_jobs},
            headers={"Content-Range": f"offres 0-{len(q2_jobs)-1}/{len(q2_jobs)}"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[resp_q1, resp_q2]),
        ):
            result = collector.collect()

        ids = [j["source_job_id"] for j in result.jobs]
        assert len(ids) == len(set(ids)), "Duplicate source_job_ids found"
        # shared job + 2 unique Q1 + 2 unique Q2 = 5
        assert len(result.jobs) == 5

    def test_optional_safety_cap_limits_total_unique_jobs(self) -> None:
        """When max_jobs is set, it acts as a global safety ceiling across all queries."""
        collector = self._make_collector(["q1", "q2", "q3"], max_jobs=6, page_size=20)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)

        # Each query returns 5 unique jobs, but global cap is 6
        def make_resp(prefix: str) -> MagicMock:
            return _mock_httpx_response(
                {"resultats": _make_jobs(5, prefix)},
                headers={"Content-Range": "offres 0-4/5"},
            )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[make_resp("A"), make_resp("B"), make_resp("C")]),
        ):
            result = collector.collect()

        assert len(result.jobs) <= 6

    def test_failed_query_does_not_abort_remaining_queries(self) -> None:
        """An API error on one query is recorded but collection continues."""
        collector = self._make_collector(["bad-query", "good-query"], max_jobs=20, page_size=10)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        error_resp = _mock_httpx_error_response(400)
        good_resp = _mock_httpx_response(
            {"resultats": _make_jobs(3, "GOOD")},
            headers={"Content-Range": "offres 0-2/3"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[error_resp, good_resp]),
        ):
            result = collector.collect()

        # Error from bad-query captured, but good-query jobs collected
        assert len(result.jobs) == 3
        assert len(result.errors) >= 1
        assert any("bad-query" in e for e in result.errors)

    def test_per_query_stats_in_metadata(self) -> None:
        """CollectorResult.metadata contains per_query_stats."""
        collector = self._make_collector(["informatique", "développement"], max_jobs=20, page_size=10)
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        resp1 = _mock_httpx_response(
            {"resultats": _make_jobs(3, "Q1")},
            headers={"Content-Range": "offres 0-2/3"},
        )
        resp2 = _mock_httpx_response(
            {"resultats": _make_jobs(4, "Q2")},
            headers={"Content-Range": "offres 0-3/4"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", side_effect=[resp1, resp2]),
        ):
            result = collector.collect()

        stats = result.metadata.get("per_query_stats", {})
        assert "informatique" in stats
        assert "développement" in stats
        assert stats["informatique"] == 3
        assert stats["développement"] == 4

    def test_fallback_informatique_when_no_discovery_queries_provided(self) -> None:
        """Without discovery_queries config, 'informatique' is the default query."""
        collector = FranceTravailCollector(
            config={"client_id": "id", "client_secret": "secret"}
        )
        token_resp = _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)
        jobs_resp = _mock_httpx_response({"resultats": []})

        with (
            patch.object(httpx.Client, "post", return_value=token_resp),
            patch.object(httpx.Client, "get", return_value=jobs_resp) as mock_get,
        ):
            collector.collect()

        params = mock_get.call_args.kwargs.get("params", {})
        assert params.get("motsCles") == _DEFAULT_SEARCH_KEYWORDS


# ---------------------------------------------------------------------------
# Tests: per-page retry and partial-result preservation
# ---------------------------------------------------------------------------


class TestFranceTravailRetryAndPartialResults:
    """Verify retry logic and partial-result preservation in _search_jobs_for_query."""

    def _make_collector(
        self,
        queries: list[str] | None = None,
        page_size: int = 10,
        rate_limit_rps: float = 0.0,
    ) -> FranceTravailCollector:
        """rate_limit_rps=0 disables rate-limiter sleeps so only retry sleeps appear."""
        return FranceTravailCollector(
            config={
                "client_id": "id",
                "client_secret": "secret",
                "discovery_queries": queries or ["q1"],
                "rate_limit_rps": rate_limit_rps,
                "max_workers": 1,  # sequential so side_effect order is deterministic
            },
            _page_size=page_size,
        )

    def _token(self) -> MagicMock:
        return _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)

    # -- Retry success --------------------------------------------------------

    def test_429_retry_succeeds(self) -> None:
        """A 429 on page 2 is retried; all jobs are collected on success."""
        collector = self._make_collector(page_size=5)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(5, "P1")},
            headers={"Content-Range": "offres 0-4/10"},
        )
        error_429 = _mock_httpx_error_response(429)
        page2 = _mock_httpx_response(
            {"resultats": _make_jobs(5, "P2")},
            headers={"Content-Range": "offres 5-9/10"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[page1, error_429, page2]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 10
        assert result.errors == []

    def test_transient_500_is_retried(self) -> None:
        """A 500 error on page 1 is retried; success on second attempt."""
        collector = self._make_collector()
        error_500 = _mock_httpx_error_response(500)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(3, "J")},
            headers={"Content-Range": "offres 0-2/3"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[error_500, page1]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 3
        assert result.errors == []

    def test_transient_503_is_retried(self) -> None:
        """A 503 error on page 1 is retried; success on second attempt."""
        collector = self._make_collector()
        error_503 = _mock_httpx_error_response(503)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(2, "K")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[error_503, page1]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 2
        assert result.errors == []

    # -- Retry-After header ---------------------------------------------------

    def test_429_with_retry_after_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On 429 with Retry-After: N, time.sleep is called with N seconds."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        collector = self._make_collector()
        error_429 = _mock_httpx_error_response(429, headers={"Retry-After": "5"})
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(2, "R")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[error_429, page1]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 2
        assert any(s == 5.0 for s in sleep_calls), f"Expected sleep(5.0) in {sleep_calls}"

    def test_429_without_retry_after_uses_exponential_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On 429 without Retry-After, exponential backoff sleep is used."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        collector = self._make_collector()
        error_429 = _mock_httpx_error_response(429)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(2, "B")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[error_429, page1]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 2
        # First retry backoff: _RETRY_BACKOFF_BASE * 2^0 = 1.0s
        assert any(s == 1.0 for s in sleep_calls), f"Expected backoff sleep(1.0) in {sleep_calls}"

    # -- Permanent error (no retry) -------------------------------------------

    def test_permanent_400_not_retried(self) -> None:
        """A 400 error is permanent — not retried, pagination stops immediately."""
        collector = self._make_collector()
        error_400 = _mock_httpx_error_response(400)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[error_400]) as mock_get,
        ):
            result = collector.collect()

        # Only one GET call: no retry
        assert mock_get.call_count == 1
        assert result.jobs == []
        assert len(result.errors) == 1

    # -- Partial preservation after retry exhaustion --------------------------

    def test_429_exhausts_all_retries_returns_partial(self) -> None:
        """When all retries are exhausted, jobs from previous pages are preserved."""
        from src.collectors.france_travail import _MAX_RETRY_ATTEMPTS
        collector = self._make_collector(page_size=5)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(5, "GOOD")},
            headers={"Content-Range": "offres 0-4/10"},
        )
        # page 2: fail on every attempt (1 initial + _MAX_RETRY_ATTEMPTS retries)
        error_429s = [_mock_httpx_error_response(429)] * (_MAX_RETRY_ATTEMPTS + 1)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[page1, *error_429s]),
        ):
            result = collector.collect()

        # Page 1 jobs preserved; page 2 failed after all retries
        assert len(result.jobs) == 5
        assert len(result.errors) >= 1

    def test_partial_results_preserved_after_failed_retries(self) -> None:
        """Explicitly verify that partial jobs are in result.jobs after a mid-query failure."""
        from src.collectors.france_travail import _MAX_RETRY_ATTEMPTS
        collector = self._make_collector(page_size=3)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(3, "PARTIAL")},
            headers={"Content-Range": "offres 0-2/6"},
        )
        error_500s = [_mock_httpx_error_response(500)] * (_MAX_RETRY_ATTEMPTS + 1)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[page1, *error_500s]),
        ):
            result = collector.collect()

        source_ids = [j["source_job_id"] for j in result.jobs]
        assert len(source_ids) == 3
        assert all("PARTIAL" in sid for sid in source_ids)

    # -- Query isolation ------------------------------------------------------

    def test_failed_query_continues_to_next_query(self) -> None:
        """A permanent error on Q1 is recorded; Q2 still runs and returns jobs."""
        collector = self._make_collector(queries=["q-bad", "q-good"], page_size=5)
        error_400 = _mock_httpx_error_response(400)
        page_good = _mock_httpx_response(
            {"resultats": _make_jobs(3, "GOOD")},
            headers={"Content-Range": "offres 0-2/3"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[error_400, page_good]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 3
        assert len(result.errors) >= 1
        assert any("q-bad" in e for e in result.errors)

    # -- per_query_status metadata --------------------------------------------

    def test_metadata_completed_status(self) -> None:
        """A query with no errors has status 'completed' in per_query_status."""
        collector = self._make_collector(queries=["q1"])
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(2, "C")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", return_value=page1),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        assert status["q1"]["status"] == "completed"
        assert status["q1"]["error"] is None

    def test_metadata_partial_status(self) -> None:
        """A query with page 1 success and page 2 failure has status 'partial'."""
        collector = self._make_collector(queries=["q1"], page_size=2)
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(2, "D")},
            headers={"Content-Range": "offres 0-1/4"},
        )
        error_400 = _mock_httpx_error_response(400)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[page1, error_400]),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        assert status["q1"]["status"] == "partial"
        assert status["q1"]["jobs"] > 0
        assert status["q1"]["error"] is not None

    def test_metadata_failed_status(self) -> None:
        """A query where page 1 fails immediately has status 'failed'."""
        collector = self._make_collector(queries=["q1"])
        error_400 = _mock_httpx_error_response(400)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", return_value=error_400),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        assert status["q1"]["status"] == "failed"
        assert status["q1"]["jobs"] == 0

    def test_final_result_includes_partial_and_completed_jobs(self) -> None:
        """Jobs from partial queries are included alongside fully completed queries."""
        collector = self._make_collector(queries=["q1", "q2"], page_size=3)
        # q1: page 1 success (3 jobs), page 2 permanent fail → partial
        q1_page1 = _mock_httpx_response(
            {"resultats": _make_jobs(3, "Q1")},
            headers={"Content-Range": "offres 0-2/6"},
        )
        q1_page2_fail = _mock_httpx_error_response(400)
        # q2: single page success (2 jobs)
        q2_page1 = _mock_httpx_response(
            {"resultats": _make_jobs(2, "Q2")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[q1_page1, q1_page2_fail, q2_page1]),
        ):
            result = collector.collect()

        assert len(result.jobs) == 5

        status = result.metadata.get("per_query_status", {})
        assert status["q1"]["status"] == "partial"
        assert status["q2"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Tests: saturated-query partitioning
# ---------------------------------------------------------------------------

# France Travail API offset limit — tests must never exceed this.
_MAX_OFFSET = 3000


def _make_pages_up_to_limit(page_size: int, total: int) -> list[MagicMock]:
    """Simulate exactly reaching _MAX_API_OFFSET with api_total=total > 3000."""
    pages: list[MagicMock] = []
    collected = 0
    start = 0
    while start < _MAX_OFFSET and collected < total:
        batch_size = min(page_size, _MAX_OFFSET - start, total - collected)
        if batch_size <= 0:
            break
        end = start + batch_size - 1
        batch = _make_jobs(batch_size, f"SAT{start:04d}")
        pages.append(_mock_httpx_response(
            {"resultats": batch},
            headers={"Content-Range": f"offres {start}-{end}/{total}"},
        ))
        collected += batch_size
        start += batch_size
    return pages


class TestFranceTravailSaturation:
    """Saturated-query detection and partition generation."""

    def _make_collector(
        self,
        queries: list[str] | None = None,
        partition_vocabulary: list[str] | None = None,
        max_partitions: int = 12,
        max_jobs: int | None = None,
        page_size: int = 150,
    ) -> FranceTravailCollector:
        cfg: dict[str, Any] = {
            "client_id": "id",
            "client_secret": "secret",
            "discovery_queries": queries or ["informatique"],
            "rate_limit_rps": 0.0,
            "max_workers": 1,  # sequential so side_effect order is deterministic
        }
        if partition_vocabulary is not None:
            cfg["partition_vocabulary"] = partition_vocabulary
        cfg["max_partitions_per_query"] = max_partitions
        if max_jobs is not None:
            cfg["max_jobs"] = max_jobs
        return FranceTravailCollector(config=cfg, _page_size=page_size)

    def _token(self) -> MagicMock:
        return _mock_httpx_response(_SAMPLE_TOKEN_RESPONSE)

    # -- Saturation detection -------------------------------------------------

    def test_api_total_above_3000_marks_query_saturated(self) -> None:
        """A query with api_total=6052 must become status='saturated'."""
        collector = self._make_collector(page_size=150)
        pages = _make_pages_up_to_limit(page_size=150, total=6052)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=pages),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        assert status["informatique"]["status"] == "saturated"
        assert status["informatique"]["saturated"] is True
        assert status["informatique"]["stop_reason"] == "api_offset_limit"

    def test_saturated_query_never_requests_start_gte_3000(self) -> None:
        """No API request may use a start offset >= 3000."""
        collector = self._make_collector(page_size=150)
        pages = _make_pages_up_to_limit(page_size=150, total=6052)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=pages) as mock_get,
        ):
            collector.collect()

        for call in mock_get.call_args_list:
            rng = call.kwargs.get("params", {}).get("range", "0-0")
            start = int(rng.split("-")[0])
            assert start < _MAX_OFFSET, f"Illegal start offset {start} in range '{rng}'"

    def test_non_saturated_query_is_completed(self) -> None:
        """A query with api_total=400 is exhausted normally — no saturation."""
        collector = self._make_collector(page_size=150)
        page = _mock_httpx_response(
            {"resultats": _make_jobs(10, "NS")},
            headers={"Content-Range": "offres 0-9/10"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", return_value=page),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        assert status["informatique"]["status"] == "completed"
        assert status["informatique"]["saturated"] is False
        assert status["informatique"]["partitions_generated"] == 0

    def test_non_saturated_query_generates_no_partitions(self) -> None:
        """A non-saturated query must not generate partition queries."""
        vocab = ["développeur", "data", "web"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        page = _mock_httpx_response(
            {"resultats": _make_jobs(5, "NS")},
            headers={"Content-Range": "offres 0-4/5"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", return_value=page) as mock_get,
        ):
            result = collector.collect()

        # Only 1 query executed (the original — no partitions)
        assert mock_get.call_count == 1
        assert len(result.jobs) == 5

    # -- Partition generation --------------------------------------------------

    def test_saturated_query_generates_partition_queries(self) -> None:
        """A saturated 'informatique' query must spawn partition queries."""
        vocab = ["développeur", "data", "cybersécurité"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        sat_pages = _make_pages_up_to_limit(page_size=150, total=6052)
        # Each partition returns a small distinct result set
        p_dev = _mock_httpx_response(
            {"resultats": _make_jobs(3, "DEV")},
            headers={"Content-Range": "offres 0-2/3"},
        )
        p_data = _mock_httpx_response(
            {"resultats": _make_jobs(2, "DAT")},
            headers={"Content-Range": "offres 0-1/2"},
        )
        p_cyber = _mock_httpx_response(
            {"resultats": _make_jobs(1, "CYB")},
            headers={"Content-Range": "offres 0-0/1"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, p_dev, p_data, p_cyber]),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        assert status["informatique"]["status"] == "saturated"
        assert status["informatique"]["partitions_generated"] == 3
        assert "informatique développeur" in status
        assert "informatique data" in status
        assert "informatique cybersécurité" in status
        assert status["informatique développeur"]["type"] == "partition"
        assert status["informatique développeur"]["parent_query"] == "informatique"

    def test_original_results_preserved_alongside_partitions(self) -> None:
        """Jobs from the saturated query plus partition jobs must all survive."""
        vocab = ["développeur", "data"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        sat_pages = _make_pages_up_to_limit(page_size=150, total=4000)
        orig_count = sum(150 for _ in range(_MAX_OFFSET // 150))
        p_dev = _mock_httpx_response(
            {"resultats": _make_jobs(5, "DEV")},
            headers={"Content-Range": "offres 0-4/5"},
        )
        p_data = _mock_httpx_response(
            {"resultats": _make_jobs(3, "DAT")},
            headers={"Content-Range": "offres 0-2/3"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, p_dev, p_data]),
        ):
            result = collector.collect()

        assert len(result.jobs) == orig_count + 5 + 3

    def test_partition_queries_are_not_duplicates_of_original(self) -> None:
        """Partition queries must differ from the original saturated query."""
        vocab = ["développeur", "data"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        partitions = collector._generate_partition_queries(
            "informatique", {"informatique"}
        )
        assert "informatique" not in partitions
        assert "informatique développeur" in partitions
        assert "informatique data" in partitions

    def test_max_partitions_per_query_is_respected(self) -> None:
        """Never generate more partitions than max_partitions_per_query."""
        vocab = [f"term{i}" for i in range(20)]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            max_partitions=5,
        )
        partitions = collector._generate_partition_queries("informatique", set())
        assert len(partitions) <= 5

    def test_duplicate_partition_queries_are_skipped(self) -> None:
        """A compound query already in the work queue must not be added again."""
        vocab = ["développeur"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
        )
        already_queued = {"informatique", "informatique développeur"}
        partitions = collector._generate_partition_queries("informatique", already_queued)
        assert "informatique développeur" not in partitions

    # -- One-level partitioning -----------------------------------------------

    def test_partition_queries_do_not_generate_sub_partitions(self) -> None:
        """Partitions must never spawn further partition queries.

        'informatique' (saturated) → 'informatique développeur' (partition).
        'informatique développeur' must NOT spawn 'informatique développeur web'.
        """
        vocab = ["développeur", "web"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        # informatique: saturated (api_total=6052)
        sat_pages = _make_pages_up_to_limit(page_size=150, total=6052)
        # informatique développeur: also saturated (api_total=4000) — but no sub-partitions
        sat_pages_dev = _make_pages_up_to_limit(page_size=150, total=4000)
        # informatique web: small, completed
        p_web = _mock_httpx_response(
            {"resultats": _make_jobs(2, "WEB")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, *sat_pages_dev, p_web]),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        # 'informatique développeur' is saturated but is a partition → no sub-partitions
        assert status["informatique développeur"]["status"] == "saturated"
        assert status["informatique développeur"]["partitions_generated"] == 0
        # No queries of depth 2 should exist
        depth_2 = [q for q in status if q.startswith("informatique développeur ")]
        assert depth_2 == []

    # -- Global deduplication -------------------------------------------------

    def test_overlapping_results_from_partitions_deduplicated(self) -> None:
        """Jobs returned by both original and partition queries appear only once."""
        vocab = ["développeur", "data"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        shared_job = _make_jobs(1, "SHARED")[0]
        sat_pages = _make_pages_up_to_limit(page_size=150, total=4000)
        p_dev = _mock_httpx_response(
            {"resultats": [shared_job, *_make_jobs(2, "ONLY_DEV")]},
            headers={"Content-Range": "offres 0-2/3"},
        )
        p_data = _mock_httpx_response(
            {"resultats": [shared_job, *_make_jobs(1, "ONLY_DATA")]},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, p_dev, p_data]),
        ):
            result = collector.collect()

        ids = [j["source_job_id"] for j in result.jobs]
        assert len(ids) == len(set(ids)), "Duplicate source_job_ids found"
        # SHARED0000 must appear exactly once
        shared_count = ids.count("SHARED0000")
        assert shared_count == 1

    # -- Rate limiter ---------------------------------------------------------

    def test_rate_limiter_shared_across_partitions(self) -> None:
        """The same _RateLimiter instance must cover original + partition requests."""
        vocab = ["développeur"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        wait_calls: list[str] = []
        original_wait = collector._rate_limiter.wait

        def recording_wait() -> None:
            wait_calls.append("wait")
            original_wait()

        collector._rate_limiter.wait = recording_wait  # type: ignore[method-assign]

        sat_pages = _make_pages_up_to_limit(page_size=150, total=4000)
        p_dev = _mock_httpx_response(
            {"resultats": _make_jobs(2, "DEV")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, p_dev]),
        ):
            collector.collect()

        # Rate limiter must have been called for every search API request
        # (original pages + partition page)
        total_api_calls = len(sat_pages) + 1
        assert len(wait_calls) >= total_api_calls

    # -- Error handling -------------------------------------------------------

    def test_partition_error_does_not_abort_other_partitions(self) -> None:
        """A 400 on partition A must not prevent partition B from running."""
        vocab = ["développeur", "data"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        sat_pages = _make_pages_up_to_limit(page_size=150, total=4000)
        p_dev_error = _mock_httpx_error_response(400)
        p_data = _mock_httpx_response(
            {"resultats": _make_jobs(3, "DAT")},
            headers={"Content-Range": "offres 0-2/3"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, p_dev_error, p_data]),
        ):
            result = collector.collect()

        # Data-partition jobs must still be present
        assert len(result.jobs) > 0
        dat_ids = [j["source_job_id"] for j in result.jobs if "DAT" in j["source_job_id"]]
        assert len(dat_ids) == 3
        # Error recorded for the failing partition
        assert any("informatique développeur" in e for e in result.errors)

    def test_no_partition_vocabulary_means_no_partitions(self) -> None:
        """Without partition vocabulary, saturation generates no follow-up queries."""
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=[],  # empty
            page_size=150,
        )
        sat_pages = _make_pages_up_to_limit(page_size=150, total=6052)

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=sat_pages) as mock_get,
        ):
            result = collector.collect()

        # Only the original pages — no partition requests
        assert mock_get.call_count == len(sat_pages)
        status = result.metadata.get("per_query_status", {})
        assert status["informatique"]["partitions_generated"] == 0

    # -- Metadata completeness ------------------------------------------------

    def test_per_query_status_has_required_fields(self) -> None:
        """per_query_status must contain the full diagnostic fields."""
        vocab = ["développeur"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            page_size=150,
        )
        sat_pages = _make_pages_up_to_limit(page_size=150, total=4000)
        p_dev = _mock_httpx_response(
            {"resultats": _make_jobs(2, "DEV")},
            headers={"Content-Range": "offres 0-1/2"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", side_effect=[*sat_pages, p_dev]),
        ):
            result = collector.collect()

        status = result.metadata.get("per_query_status", {})
        for query, meta in status.items():
            assert "status" in meta, f"Missing 'status' for {query}"
            assert "type" in meta, f"Missing 'type' for {query}"
            assert "parent_query" in meta, f"Missing 'parent_query' for {query}"
            assert "jobs" in meta, f"Missing 'jobs' for {query}"
            assert "pages" in meta, f"Missing 'pages' for {query}"
            assert "api_total" in meta, f"Missing 'api_total' for {query}"
            assert "saturated" in meta, f"Missing 'saturated' for {query}"
            assert "partitions_generated" in meta, f"Missing 'partitions_generated' for {query}"
            assert "stop_reason" in meta, f"Missing 'stop_reason' for {query}"
            assert "error" in meta, f"Missing 'error' for {query}"

        # Original query metadata
        orig = status["informatique"]
        assert orig["type"] == "discovery"
        assert orig["parent_query"] is None
        assert orig["saturated"] is True
        assert orig["partitions_generated"] == 1

        # Partition metadata
        part = status["informatique développeur"]
        assert part["type"] == "partition"
        assert part["parent_query"] == "informatique"
        assert part["status"] == "completed"

    def test_global_max_jobs_respected_across_partitions(self) -> None:
        """Global max_jobs ceiling must apply to original + partition totals."""
        vocab = ["développeur", "data", "web"]
        collector = self._make_collector(
            queries=["informatique"],
            partition_vocabulary=vocab,
            max_jobs=5,
            page_size=150,
        )
        # With max_jobs=5 the collector caps at 5 total jobs across all queries.
        page1 = _mock_httpx_response(
            {"resultats": _make_jobs(5, "SAT")},
            headers={"Content-Range": "offres 0-4/6000"},
        )

        with (
            patch.object(httpx.Client, "post", return_value=self._token()),
            patch.object(httpx.Client, "get", return_value=page1),
        ):
            result = collector.collect()

        assert len(result.jobs) <= 5
