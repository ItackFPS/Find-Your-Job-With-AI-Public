"""Unit tests for the APEC httpx-based collector.

All tests are offline — no network requests are made.  httpx is mocked via
unittest.mock.patch so the suite runs without live internet access.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from src.collectors.apec import APECCollector, _map_job_to_raw, _parse_location

# ---------------------------------------------------------------------------
# Helpers: fake API responses
# ---------------------------------------------------------------------------


def _make_response(
    jobs: list[dict[str, Any]],
    total: int | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Return a mock httpx.Response for a rechercheOffre call."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.headers = {}
    if status_code == 200:
        mock.json.return_value = {
            "totalCount": total if total is not None else len(jobs),
            "resultats": jobs,
        }
        mock.raise_for_status.return_value = None
    else:
        mock.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
        mock.text = f"Error {status_code}"
    return mock


def _make_job(
    numero: str = "179000001W",
    title: str = "Dev Backend F/H",
    company: str = "TechCo",
    lieu: str = "Paris - 75",
    tt_code: int | None = None,
    date: str = "2026-08-27T10:00:00.000+0000",
    description: str = "Great opportunity.",
) -> dict[str, Any]:
    job: dict[str, Any] = {
        "numeroOffre": numero,
        "intitule": title,
        "nomCommercial": company,
        "lieuTexte": lieu,
        "texteOffre": description,
        "datePublication": date,
        "typeContrat": 101888,
    }
    if tt_code is not None:
        job["idNomTeletravail"] = tt_code
    return job


# ---------------------------------------------------------------------------
# Tests: _parse_location
# ---------------------------------------------------------------------------


class TestParseLocation:
    def test_city_with_dept(self) -> None:
        city, dept = _parse_location("Paris - 75")
        assert city == "Paris"
        assert dept == "75"

    def test_city_with_dom_dept(self) -> None:
        city, dept = _parse_location("Cayenne - 973")
        assert city == "Cayenne"
        assert dept == "973"

    def test_city_with_corse(self) -> None:
        city, dept = _parse_location("Ajaccio - 2A")
        assert city == "Ajaccio"
        assert dept == "2A"

    def test_city_no_dept(self) -> None:
        city, dept = _parse_location("Lyon")
        assert city == "Lyon"
        assert dept is None

    def test_city_with_dash_no_dept(self) -> None:
        # Suffix is not a valid dept code
        _city, dept = _parse_location("Villeneuve-sur-Lot - Lot-et-Garonne")
        assert dept is None

    def test_empty_string(self) -> None:
        city, dept = _parse_location("")
        assert city == ""
        assert dept is None

    def test_city_containing_dash(self) -> None:
        # rsplit picks last " - " so city keeps its own dashes
        city, dept = _parse_location("Villeneuve-d'Ascq - 59")
        assert city == "Villeneuve-d'Ascq"
        assert dept == "59"


# ---------------------------------------------------------------------------
# Tests: _map_job_to_raw
# ---------------------------------------------------------------------------


class TestMapJobToRaw:
    def test_full_mapping(self) -> None:
        job = _make_job(numero="179001W", title="Lead Dev", company="Acme", lieu="Bordeaux - 33",
                        tt_code=20766, date="2026-08-20T08:00:00.000+0000")
        raw = _map_job_to_raw(job)
        assert raw["title"] == "Lead Dev"
        assert raw["company"] == "Acme"
        assert raw["location"] == "Bordeaux"
        assert raw["department_code"] == "33"
        assert raw["countries"] == ["France"]
        assert raw["work_mode"] == "hybrid"
        assert raw["remote"] is False
        assert raw["published_at"] == "2026-08-20T08:00:00.000+0000"
        assert raw["source_job_id"] == "179001W"
        assert "apec.fr" in raw["canonical_url"]
        assert "179001W" in raw["canonical_url"]
        assert raw["type"] is None  # not decodable from typeContrat

    def test_on_site_work_mode(self) -> None:
        raw = _map_job_to_raw(_make_job(tt_code=20765))
        assert raw["work_mode"] == "on_site"
        assert raw["remote"] is False

    def test_hybrid_work_mode(self) -> None:
        raw = _map_job_to_raw(_make_job(tt_code=20766))
        assert raw["work_mode"] == "hybrid"

    def test_unknown_tt_code_gives_none(self) -> None:
        # 20949 is an observed but unconfirmed code → None
        raw = _map_job_to_raw(_make_job(tt_code=20949))
        assert raw["work_mode"] is None

    def test_absent_tt_code_gives_none(self) -> None:
        raw = _map_job_to_raw(_make_job(tt_code=None))
        assert raw["work_mode"] is None

    def test_missing_company_becomes_none(self) -> None:
        job = _make_job()
        job["nomCommercial"] = ""
        raw = _map_job_to_raw(job)
        assert raw["company"] is None

    def test_missing_numero_gives_no_url(self) -> None:
        job = _make_job()
        job["numeroOffre"] = None
        raw = _map_job_to_raw(job)
        assert raw["canonical_url"] is None
        assert raw["source_job_id"] is None

    def test_location_without_dept(self) -> None:
        raw = _map_job_to_raw(_make_job(lieu="Remote"))
        assert raw["location"] == "Remote"
        assert raw["department_code"] is None

    def test_skills_always_empty(self) -> None:
        assert _map_job_to_raw(_make_job())["skills"] == []


# ---------------------------------------------------------------------------
# Tests: APECCollector.collect (httpx mocked)
# ---------------------------------------------------------------------------


class TestAPECCollectorSinglePage:
    @patch("src.collectors.apec.httpx.post")
    def test_returns_jobs_from_page_one(self, mock_post: MagicMock) -> None:
        jobs = [_make_job(f"1000{i}W", f"Job {i}") for i in range(5)]
        mock_post.return_value = _make_response(jobs, total=50)
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert result.source_name == "apec"
        assert len(result.jobs) == 5
        assert result.errors == []

    @patch("src.collectors.apec.httpx.post")
    def test_job_fields_mapped_correctly(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_response(
            [_make_job(numero="179001W", title="Lead Dev", company="Acme",
                       lieu="Lyon - 69", tt_code=20766)]
        )
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        job = result.jobs[0]
        assert job["title"] == "Lead Dev"
        assert job["company"] == "Acme"
        assert job["location"] == "Lyon"
        assert job["department_code"] == "69"
        assert job["work_mode"] == "hybrid"
        assert job["source_job_id"] == "179001W"

    @patch("src.collectors.apec.httpx.post")
    def test_empty_title_job_excluded(self, mock_post: MagicMock) -> None:
        jobs = [_make_job(title=""), _make_job(numero="2W", title="Real Job")]
        mock_post.return_value = _make_response(jobs)
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert len(result.jobs) == 1
        assert result.jobs[0]["title"] == "Real Job"


class TestAPECCollectorPagination:
    @patch("src.collectors.apec.httpx.post")
    def test_fetches_multiple_pages(self, mock_post: MagicMock) -> None:
        page1 = [_make_job(f"1000{i}W") for i in range(20)]
        page2 = [_make_job(f"2000{i}W") for i in range(20)]
        mock_post.side_effect = [
            _make_response(page1, total=40),
            _make_response(page2, total=40),
        ]
        col = APECCollector(config={"max_pages": 2, "page_size": 20, "rate_limit_delay": 0})
        result = col.collect()
        assert len(result.jobs) == 40
        assert mock_post.call_count == 2

    @patch("src.collectors.apec.httpx.post")
    def test_stops_at_max_pages(self, mock_post: MagicMock) -> None:
        full_page = [_make_job(f"{i}W") for i in range(20)]
        mock_post.return_value = _make_response(full_page, total=1000)
        col = APECCollector(config={"max_pages": 2, "page_size": 20, "rate_limit_delay": 0})
        col.collect()
        assert mock_post.call_count == 2

    @patch("src.collectors.apec.httpx.post")
    def test_stops_early_on_last_page(self, mock_post: MagicMock) -> None:
        page1 = [_make_job(f"1{i}W") for i in range(20)]
        page2 = [_make_job(f"2{i}W") for i in range(5)]  # partial → last page
        mock_post.side_effect = [
            _make_response(page1, total=25),
            _make_response(page2, total=25),
        ]
        col = APECCollector(config={"max_pages": 5, "page_size": 20, "rate_limit_delay": 0})
        result = col.collect()
        assert len(result.jobs) == 25
        assert mock_post.call_count == 2

    @patch("src.collectors.apec.httpx.post")
    def test_startindex_increments_correctly(self, mock_post: MagicMock) -> None:
        full_page = [_make_job(f"{i}W") for i in range(20)]
        mock_post.side_effect = [
            _make_response(full_page, total=100),
            _make_response([], total=100),
        ]
        col = APECCollector(config={"max_pages": 2, "page_size": 20, "rate_limit_delay": 0})
        col.collect()
        calls = mock_post.call_args_list
        assert calls[0].kwargs["json"]["pagination"]["startIndex"] == 0
        assert calls[1].kwargs["json"]["pagination"]["startIndex"] == 20


class TestAPECCollectorDeduplication:
    @patch("src.collectors.apec.httpx.post")
    def test_duplicate_across_pages_counted_once(self, mock_post: MagicMock) -> None:
        # Same numeroOffre appears on both pages (e.g. freshly published job).
        # Page 1 must be full (== page_size) so the collector does not stop early.
        dup = _make_job(numero="DUP001W", title="Dup Job")
        page1 = [_make_job(f"U{i}W") for i in range(19)] + [dup]   # 20 jobs (full)
        page2 = [dup, _make_job(numero="B1W")]                       # dup + 1 new
        mock_post.side_effect = [
            _make_response(page1, total=21),
            _make_response(page2, total=21),
        ]
        col = APECCollector(config={"max_pages": 2, "page_size": 20, "rate_limit_delay": 0})
        result = col.collect()
        ids = [j["source_job_id"] for j in result.jobs]
        assert ids.count("DUP001W") == 1
        assert len(result.jobs) == 21  # 19 unique + DUP001W + B1W

    @patch("src.collectors.apec.httpx.post")
    def test_no_duplicates_within_same_page(self, mock_post: MagicMock) -> None:
        job = _make_job(numero="X1W")
        mock_post.return_value = _make_response([job, job], total=2)
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert len(result.jobs) == 1


class TestAPECCollectorErrors:
    @patch("src.collectors.apec.httpx.post")
    def test_network_error_returns_error_no_crash(self, mock_post: MagicMock) -> None:
        import httpx as _httpx
        mock_post.side_effect = _httpx.ConnectError("Connection refused")
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0, "retries": 1})
        result = col.collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "Connection refused" in result.errors[0]

    @patch("src.collectors.apec.httpx.post")
    def test_timeout_returns_error(self, mock_post: MagicMock) -> None:
        import httpx as _httpx
        mock_post.side_effect = _httpx.TimeoutException("timeout")
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0, "retries": 1})
        result = col.collect()
        assert result.jobs == []
        assert result.errors

    @patch("src.collectors.apec.httpx.post")
    def test_http_500_retries_then_errors(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_response([], status_code=500)
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0, "retries": 2})
        result = col.collect()
        assert result.jobs == []
        assert result.errors

    @patch("src.collectors.apec.httpx.post")
    def test_failed_page_stops_pagination(self, mock_post: MagicMock) -> None:
        import httpx as _httpx
        good_page = [_make_job(f"{i}W") for i in range(20)]
        mock_post.side_effect = [
            _make_response(good_page, total=100),
            _httpx.ConnectError("dropped"),
        ]
        col = APECCollector(config={"max_pages": 3, "rate_limit_delay": 0, "retries": 1})
        result = col.collect()
        assert len(result.jobs) == 20
        assert len(result.errors) == 1

    @patch("src.collectors.apec.httpx.post")
    def test_malformed_response_no_resultats(self, mock_post: MagicMock) -> None:
        mock = MagicMock()
        mock.status_code = 200
        mock.raise_for_status.return_value = None
        mock.json.return_value = {"message": "unexpected", "totalCount": 0}
        mock_post.return_value = mock
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert result.jobs == []
        assert result.errors == []  # empty resultats is not an error

    @patch("src.collectors.apec.httpx.post")
    def test_second_page_fails_partial_result_returned(self, mock_post: MagicMock) -> None:
        import httpx as _httpx
        good = [_make_job(f"{i}W") for i in range(20)]
        mock_post.side_effect = [
            _make_response(good, total=40),
            _httpx.ConnectError("network lost"),
        ]
        col = APECCollector(config={"max_pages": 2, "rate_limit_delay": 0, "retries": 1})
        result = col.collect()
        assert len(result.jobs) == 20
        assert result.errors


class TestAPECCollectorWorkMode:
    @patch("src.collectors.apec.httpx.post")
    def test_on_site_detected(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_response([_make_job(tt_code=20765)])
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert result.jobs[0]["work_mode"] == "on_site"

    @patch("src.collectors.apec.httpx.post")
    def test_hybrid_detected(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_response([_make_job(tt_code=20766)])
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert result.jobs[0]["work_mode"] == "hybrid"


# ---------------------------------------------------------------------------
# Tests: APECCollector — multi-query (discovery_queries / motsCles)
# ---------------------------------------------------------------------------


class TestAPECCollectorMultiQuery:
    """Verify multi-keyword search via discovery_queries config key."""

    @patch("src.collectors.apec.httpx.post")
    def test_one_request_per_keyword(self, mock_post: MagicMock) -> None:
        """One full paginated cycle (limited to max_pages=1) per keyword."""
        mock_post.return_value = _make_response(
            [_make_job(f"K{i}W") for i in range(5)], total=5
        )
        col = APECCollector(config={
            "max_pages": 1,
            "rate_limit_delay": 0,
            "discovery_queries": ["informatique", "développeur"],
        })
        result = col.collect()
        # 2 keywords × 1 page each = 2 POST calls
        assert mock_post.call_count == 2
        assert result.errors == []

    @patch("src.collectors.apec.httpx.post")
    def test_motscles_field_sent_per_keyword(self, mock_post: MagicMock) -> None:
        """Each keyword is forwarded as 'motsCles' in the POST body."""
        mock_post.return_value = _make_response([], total=0)
        col = APECCollector(config={
            "max_pages": 1,
            "rate_limit_delay": 0,
            "discovery_queries": ["data", "devops"],
        })
        col.collect()
        keywords_sent = [
            call.kwargs["json"].get("motsCles")
            for call in mock_post.call_args_list
        ]
        assert "data" in keywords_sent
        assert "devops" in keywords_sent

    @patch("src.collectors.apec.httpx.post")
    def test_fallback_no_motscles_when_no_queries(self, mock_post: MagicMock) -> None:
        """No discovery_queries → blank search (motsCles absent from POST body)."""
        mock_post.return_value = _make_response(
            [_make_job("BLK01W")], total=1
        )
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert mock_post.call_count == 1
        assert "motsCles" not in mock_post.call_args.kwargs["json"]
        assert len(result.jobs) == 1

    @patch("src.collectors.apec.httpx.post")
    def test_deduplication_across_keywords(self, mock_post: MagicMock) -> None:
        """Same numeroOffre appearing under different keywords is counted once."""
        shared = _make_job(numero="DUP999W", title="Shared Job")
        mock_post.side_effect = [
            _make_response([shared, _make_job("A1W")], total=2),
            _make_response([shared, _make_job("B1W")], total=2),
        ]
        col = APECCollector(config={
            "max_pages": 1,
            "rate_limit_delay": 0,
            "discovery_queries": ["informatique", "développeur"],
        })
        result = col.collect()
        ids = [j["source_job_id"] for j in result.jobs]
        assert ids.count("DUP999W") == 1
        # 1 shared (deduped) + 1 unique per keyword = 3 total
        assert len(result.jobs) == 3

    @patch("src.collectors.apec.httpx.post")
    def test_partial_keyword_failure_continues(self, mock_post: MagicMock) -> None:
        """If one keyword fails, the others still run and their jobs are returned."""
        import httpx as _httpx
        mock_post.side_effect = [
            _httpx.ConnectError("network down"),         # keyword 1 fails
            _make_response([_make_job("OK01W")], total=1),  # keyword 2 succeeds
        ]
        col = APECCollector(config={
            "max_pages": 1,
            "rate_limit_delay": 0,
            "retries": 1,
            "discovery_queries": ["informatique", "développeur"],
        })
        result = col.collect()
        assert len(result.jobs) == 1
        assert len(result.errors) == 1
        assert "informatique" in result.errors[0]

    @patch("src.collectors.apec.httpx.post")
    def test_jobs_aggregated_across_keywords(self, mock_post: MagicMock) -> None:
        """Jobs from all keywords are merged into a single result list."""
        kw1_jobs = [_make_job(f"K1{i}W") for i in range(3)]
        kw2_jobs = [_make_job(f"K2{i}W") for i in range(4)]
        mock_post.side_effect = [
            _make_response(kw1_jobs, total=3),
            _make_response(kw2_jobs, total=4),
        ]
        col = APECCollector(config={
            "max_pages": 1,
            "rate_limit_delay": 0,
            "discovery_queries": ["python", "devops"],
        })
        result = col.collect()
        assert len(result.jobs) == 7
        assert result.errors == []

    @patch("src.collectors.apec.httpx.post")
    def test_unconfirmed_code_is_none(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_response([_make_job(tt_code=20949)])
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert result.jobs[0]["work_mode"] is None

    @patch("src.collectors.apec.httpx.post")
    def test_absent_tt_code_is_none(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_response([_make_job(tt_code=None)])
        col = APECCollector(config={"max_pages": 1, "rate_limit_delay": 0})
        result = col.collect()
        assert result.jobs[0]["work_mode"] is None
