"""Unit tests for the EURES collector (src/collectors/eures.py).

All network calls are mocked — no live requests are made.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.collectors.eures import _ISO2_TO_COUNTRY, EURESCollector, _map_job

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_jv(
    id_: str = "ABCD1234",
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    description: str = "A great job.",
    location_map: dict | None = None,
    creation_date_ms: int = 1_700_000_000_000,
) -> dict:
    return {
        "id": id_,
        "title": title,
        "description": description,
        "employer": {"name": company, "legalID": None, "website": None},
        "creationDate": creation_date_ms,
        "lastModificationDate": creation_date_ms,
        "numberOfPosts": 1,
        "locationMap": location_map if location_map is not None else {"FR": "FRJ22"},
        "euresFlag": False,
        "jobCategoriesCodes": [],
        "positionScheduleCodes": ["fulltime"],
        "positionOfferingCode": "directhire",
        "availableLanguages": ["fr"],
        "score": None,
    }


def _make_api_response(jvs: list[dict], total: int | None = None) -> dict:
    return {
        "numberRecords": total if total is not None else len(jvs),
        "jvs": jvs,
        "facets": {},
    }


def _mock_post_response(data: dict, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = data
    mock.headers = {}
    mock.raise_for_status = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# _map_job unit tests
# ---------------------------------------------------------------------------

class TestMapJob:
    def test_basic_french_job(self):
        jv = _make_jv(
            id_="JOB1",
            title="  Développeur Python  ",
            company="Startup SAS",
            description="<p>CDI Paris</p>",
            location_map={"FR": "FRJ22"},
            creation_date_ms=1_787_000_000_000,
        )
        result = _map_job(jv)
        assert result["title"] == "Développeur Python"
        assert result["company"] == "Startup SAS"
        assert result["description"] == "<p>CDI Paris</p>"
        assert result["location"] == "France"
        assert result["countries"] == ["France"]
        assert result["source_job_id"] == "JOB1"
        assert result["work_mode"] is None
        assert result["remote"] is False
        assert result["canonical_url"] == "https://europa.eu/eures/portal/jv-se/jv-details/JOB1?lang=en"
        assert result["skills"] == []

    def test_published_at_conversion(self):
        ms = 1_700_000_000_000  # 2023-11-15 in UTC
        result = _map_job(_make_jv(creation_date_ms=ms))
        parsed = datetime.fromisoformat(result["published_at"])
        assert parsed.year == 2023
        assert parsed.month == 11

    def test_missing_creation_date(self):
        jv = _make_jv()
        del jv["creationDate"]
        result = _map_job(jv)
        assert result["published_at"] is None

    def test_multi_country_location_map(self):
        jv = _make_jv(location_map={"DE": "DE212", "AT": "AT111"})
        result = _map_job(jv)
        assert "Germany" in result["countries"]
        assert "Austria" in result["countries"]
        assert result["location"] in {"Germany", "Austria"}

    def test_unknown_iso_code_kept_as_is(self):
        jv = _make_jv(location_map={"ZZ": "ZZ001"})
        result = _map_job(jv)
        assert "ZZ" in result["countries"]

    def test_empty_location_map(self):
        jv = _make_jv(location_map={})
        result = _map_job(jv)
        assert result["countries"] == []
        assert result["location"] is None

    def test_missing_employer(self):
        jv = _make_jv()
        jv["employer"] = None
        result = _map_job(jv)
        assert result["company"] is None

    def test_empty_title_gives_empty_string(self):
        jv = _make_jv(title="")
        result = _map_job(jv)
        assert result["title"] == ""

    def test_type_always_none(self):
        result = _map_job(_make_jv())
        assert result["type"] is None

    def test_non_renseigne_sentinel_becomes_none(self):
        """Regression: EURES returns 'Non renseigné' when the employer is
        unknown. This sentinel must be converted to None, not stored as the
        company name."""
        for sentinel in ("Non renseigné", "Non renseigne", "non renseigné"):
            jv = _make_jv(company=sentinel)
            result = _map_job(jv)
            assert result["company"] is None, (
                f"Expected company=None for sentinel '{sentinel}', got {result['company']!r}"
            )

    def test_real_company_name_preserved(self):
        """A real company name close to the sentinel spelling must not be dropped."""
        jv = _make_jv(company="Renseigné Solutions")
        result = _map_job(jv)
        assert result["company"] == "Renseigné Solutions"

    # -- canonical_url regression tests --------------------------------------

    def test_canonical_url_constructed_from_id(self):
        """Regression: EURES job ID must produce a clickable portal link."""
        jv = _make_jv(id_="EURES_BO_99999")
        result = _map_job(jv)
        assert result["canonical_url"] == (
            "https://europa.eu/eures/portal/jv-se/jv-details/EURES_BO_99999?lang=en"
        )

    def test_canonical_url_none_when_id_absent(self):
        """Jobs without an id must not produce a malformed link."""
        jv = _make_jv(id_="")
        result = _map_job(jv)
        assert result["canonical_url"] is None


# ---------------------------------------------------------------------------
# EURESCollector unit tests
# ---------------------------------------------------------------------------

class TestEURESCollector:
    def _collector(self, **cfg_overrides) -> EURESCollector:
        cfg = {
            "max_pages_per_query": 2,
            "results_per_page": 5,
            "rate_limit_delay": 0,
            "timeout": 10,
            "queries": [{"keyword": "developer", "location_codes": ["fr"]}],
        }
        cfg.update(cfg_overrides)
        return EURESCollector(config=cfg)

    @patch("src.collectors.eures.httpx.post")
    def test_successful_single_page(self, mock_post):
        jvs = [_make_jv(id_=str(i)) for i in range(3)]
        mock_post.return_value = _mock_post_response(_make_api_response(jvs, total=3))

        col = self._collector()
        result = col.collect()

        assert len(result.jobs) == 3
        assert len(result.errors) == 0
        assert result.jobs[0]["source_job_id"] == "0"

    @patch("src.collectors.eures.httpx.post")
    def test_pagination_stops_when_fewer_than_page_size(self, mock_post):
        # Page 1: full (5), Page 2: partial (2) → stop
        page1 = [_make_jv(id_=str(i)) for i in range(5)]
        page2 = [_make_jv(id_=str(i)) for i in range(10, 12)]
        mock_post.side_effect = [
            _mock_post_response(_make_api_response(page1, total=7)),
            _mock_post_response(_make_api_response(page2, total=7)),
        ]

        col = self._collector()
        result = col.collect()

        assert len(result.jobs) == 7
        assert mock_post.call_count == 2

    @patch("src.collectors.eures.httpx.post")
    def test_max_pages_per_query_caps_requests(self, mock_post):
        # Every page is full → should stop at max_pages_per_query=2
        page1 = [_make_jv(id_=str(i)) for i in range(5)]
        page2 = [_make_jv(id_=str(i)) for i in range(100, 105)]
        mock_post.side_effect = [
            _mock_post_response(_make_api_response(page1, total=1000)),
            _mock_post_response(_make_api_response(page2, total=1000)),
        ]

        col = self._collector(max_pages_per_query=2)
        result = col.collect()

        assert mock_post.call_count == 2
        assert len(result.jobs) == 10  # 2 pages × 5 jobs

    @patch("src.collectors.eures.httpx.post")
    def test_duplicate_ids_skipped(self, mock_post):
        jv = _make_jv(id_="SAME")
        mock_post.return_value = _mock_post_response(
            _make_api_response([jv, jv, jv], total=3)
        )

        col = self._collector()
        result = col.collect()

        assert len(result.jobs) == 1

    @patch("src.collectors.eures.httpx.post")
    def test_multiple_queries_aggregated(self, mock_post):
        # Different queries run concurrently (see EURESCollector.collect()),
        # so the mock must respond based on REQUEST CONTENT rather than call
        # order — dispatch order between independent queries is not part of
        # the collector's contract (only the final aggregated set is).
        jv_a = _make_jv(id_="A", title="Python Dev")
        jv_b = _make_jv(id_="B", title="Java Dev")

        def _side_effect(*args, **kwargs):
            keywords = kwargs["json"]["keywords"]
            kw = keywords[0]["keyword"] if keywords else None
            if kw == "python":
                return _mock_post_response(_make_api_response([jv_a], total=1))
            if kw == "java":
                return _mock_post_response(_make_api_response([jv_b], total=1))
            raise AssertionError(f"unexpected keyword: {kw}")

        mock_post.side_effect = _side_effect

        col = self._collector(
            queries=[
                {"keyword": "python", "location_codes": ["fr"]},
                {"keyword": "java", "location_codes": ["de"]},
            ]
        )
        result = col.collect()

        assert len(result.jobs) == 2
        titles = {j["title"] for j in result.jobs}
        assert "Python Dev" in titles
        assert "Java Dev" in titles

    @patch("src.collectors.eures.httpx.post")
    def test_cross_query_deduplication(self, mock_post):
        jv = _make_jv(id_="SHARED")
        mock_post.return_value = _mock_post_response(_make_api_response([jv], total=1))

        col = self._collector(
            queries=[
                {"keyword": "python", "location_codes": ["fr"]},
                {"keyword": "java", "location_codes": ["fr"]},
            ]
        )
        result = col.collect()

        # Same id returned by both queries → counted only once
        assert len(result.jobs) == 1

    @patch("src.collectors.eures.httpx.post")
    def test_network_error_recorded_continues_next_query(self, mock_post):
        # Content-based side_effect: queries run concurrently, so which one
        # is dispatched first is not guaranteed — only that EACH query's own
        # outcome (error vs success) is independent of the others.
        import httpx as _httpx
        jv_b = _make_jv(id_="B")

        def _side_effect(*args, **kwargs):
            keywords = kwargs["json"]["keywords"]
            kw = keywords[0]["keyword"] if keywords else None
            if kw == "python":
                raise _httpx.ConnectError("connection refused")
            if kw == "java":
                return _mock_post_response(_make_api_response([jv_b], total=1))
            raise AssertionError(f"unexpected keyword: {kw}")

        mock_post.side_effect = _side_effect

        col = self._collector(
            retries=1,
            queries=[
                {"keyword": "python", "location_codes": ["fr"]},
                {"keyword": "java", "location_codes": ["fr"]},
            ],
        )
        result = col.collect()

        assert len(result.errors) == 1
        assert "connection refused" in result.errors[0]
        assert len(result.jobs) == 1
        assert len(result.jobs) == 1

    @patch("src.collectors.eures.httpx.post")
    def test_http_500_retried_then_error(self, mock_post):
        bad = _mock_post_response({}, status_code=500)
        bad.raise_for_status = MagicMock()
        mock_post.return_value = bad

        col = self._collector(retries=2, max_pages_per_query=1)
        result = col.collect()

        assert len(result.errors) == 1
        assert "HTTP 500" in result.errors[0]

    @patch("src.collectors.eures.httpx.post")
    def test_http_307_sorry_page_retried_then_error(self, mock_post):
        """europa.eu redirects to sorry.ec.europa.eu (307) on overload/maintenance."""
        bad = _mock_post_response({}, status_code=307)
        bad.raise_for_status = MagicMock()
        mock_post.return_value = bad

        col = self._collector(retries=2, max_pages_per_query=1)
        result = col.collect()

        assert len(result.errors) == 1
        assert "HTTP 307" in result.errors[0]
        assert mock_post.call_count == len(col.queries) * 2

    @patch("src.collectors.eures.httpx.post")
    def test_malformed_response_missing_jvs_key(self, mock_post):
        mock_post.return_value = _mock_post_response({"numberRecords": 0})

        col = self._collector()
        result = col.collect()

        # Empty jvs → 0 jobs, no crash
        assert len(result.jobs) == 0

    @patch("src.collectors.eures.httpx.post")
    def test_job_with_empty_title_not_added(self, mock_post):
        jvs = [_make_jv(id_="A", title=""), _make_jv(id_="B", title="Real Title")]
        mock_post.return_value = _mock_post_response(_make_api_response(jvs))

        col = self._collector()
        result = col.collect()

        assert len(result.jobs) == 1
        assert result.jobs[0]["title"] == "Real Title"

    @patch("src.collectors.eures.httpx.post")
    def test_countries_populated_from_location_map(self, mock_post):
        jv = _make_jv(location_map={"BE": "BE100", "LU": "LU000"})
        mock_post.return_value = _mock_post_response(_make_api_response([jv]))

        col = self._collector()
        result = col.collect()

        countries = result.jobs[0]["countries"]
        assert "Belgium" in countries
        assert "Luxembourg" in countries

    @patch("src.collectors.eures.httpx.post")
    def test_work_mode_always_none(self, mock_post):
        jv = _make_jv(description="remote work possible")
        mock_post.return_value = _mock_post_response(_make_api_response([jv]))

        col = self._collector()
        result = col.collect()

        assert result.jobs[0]["work_mode"] is None

    @patch("src.collectors.eures.httpx.post")
    def test_source_job_id_matches_api_id(self, mock_post):
        jv = _make_jv(id_="UniqueEURES123")
        mock_post.return_value = _mock_post_response(_make_api_response([jv]))

        col = self._collector()
        result = col.collect()

        assert result.jobs[0]["source_job_id"] == "UniqueEURES123"

    @patch("src.collectors.eures.httpx.post")
    def test_result_contract(self, mock_post):
        jvs = [_make_jv()]
        mock_post.return_value = _mock_post_response(_make_api_response(jvs))

        col = self._collector()
        result = col.collect()

        assert result.source_name == "eures"
        assert isinstance(result.jobs, list)
        assert isinstance(result.errors, list)
        assert result.duration_seconds >= 0

    def test_default_queries_used_when_config_omits_queries(self):
        col = EURESCollector(config={})
        from src.collectors.eures import _DEFAULT_QUERIES
        assert col.queries is _DEFAULT_QUERIES

    def test_results_per_page_capped_at_50(self):
        col = EURESCollector(config={"results_per_page": 999})
        assert col.results_per_page == 50


# ---------------------------------------------------------------------------
# ISO-2 mapping smoke test
# ---------------------------------------------------------------------------

class TestIso2ToCountry:
    @pytest.mark.parametrize("code,expected", [
        ("FR", "France"),
        ("DE", "Germany"),
        ("BE", "Belgium"),
        ("NL", "Netherlands"),
        ("SE", "Sweden"),
        ("AT", "Austria"),
        ("CH", "Switzerland"),
        ("ES", "Spain"),
        ("IT", "Italy"),
        ("PL", "Poland"),
        ("EL", "Greece"),   # EU convention for Greece
        ("GR", "Greece"),
        ("NO", "Norway"),
        ("DK", "Denmark"),
        ("FI", "Finland"),
        ("IE", "Ireland"),
        ("CZ", "Czech Republic"),
        ("LU", "Luxembourg"),
    ])
    def test_country_mapping(self, code, expected):
        assert _ISO2_TO_COUNTRY[code] == expected
