"""Integration-style tests for IndeedCollector using mocked httpx.

All tests intercept the httpx.post call — no real network access.
Exercises: pagination, rate limiting, error handling, deduplication,
stopping conditions, and collector result structure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.collectors.indeed import API_KEY_ENV, IndeedCollector


@pytest.fixture(autouse=True)
def _indeed_api_key(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "test-indeed-key")

# ---------------------------------------------------------------------------
# Helpers for building mock GraphQL responses
# ---------------------------------------------------------------------------


def _make_job(key: str, title: str = "Dev Job", postal: str = "75001", country: str = "FR") -> dict:
    """Return a minimal GraphQL job object."""
    return {
        "key": key,
        "title": title,
        "datePublished": 1756000000000,
        "description": {"html": f"<p>Description for {title}</p>"},
        "location": {
            "countryName": "France",
            "countryCode": country,
            "admin1Code": "IDF",
            "city": "Paris",
            "postalCode": postal,
            "formatted": {"short": "Paris", "long": f"{postal} Paris"},
        },
        "compensation": None,
        "attributes": [{"key": "CF3CP", "label": "Temps plein"}],
        "employer": {"relativeCompanyPageUrl": "/cmp/AcmeCorp", "name": "Acme Corp"},
        "recruit": {"viewJobUrl": f"https://apply.indeed.com/{key}", "workSchedule": "Full-time"},
    }


def _make_response(jobs: list[dict], next_cursor: str | None = None) -> dict:
    """Wrap jobs in the expected GraphQL envelope."""
    return {
        "data": {
            "jobSearch": {
                "pageInfo": {"nextCursor": next_cursor},
                "results": [{"job": j} for j in jobs],
            }
        }
    }


def _mock_post(responses: list[dict]) -> MagicMock:
    """Build a mock for httpx.post that returns successive responses."""
    call_count = 0
    mock = MagicMock()

    def side_effect(*args, **kwargs):
        nonlocal call_count
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        if call_count < len(responses):
            resp.json.return_value = responses[call_count]
        else:
            resp.json.return_value = _make_response([])
        call_count += 1
        return resp

    mock.side_effect = side_effect
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _collector(extra_cfg: dict | None = None) -> IndeedCollector:
    cfg = {
        "queries": [{"keyword": "python", "location": "France"}],
        "max_pages_per_query": 3,
        "rate_limit_delay": 0,  # disable real sleeping in tests
        "timeout": 5,
    }
    if extra_cfg:
        cfg.update(extra_cfg)
    return IndeedCollector(cfg)


# ---------------------------------------------------------------------------
# Basic collection
# ---------------------------------------------------------------------------


class TestIndeedCollectorBasic:
    def test_collects_jobs_from_single_page(self):
        jobs = [_make_job(f"job{i}") for i in range(5)]
        responses = [_make_response(jobs, next_cursor=None)]

        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()

        assert result.source_name == "indeed"
        assert len(result.jobs) == 5
        assert result.errors == []

    def test_collects_across_two_pages(self):
        page1 = [_make_job(f"j1_{i}") for i in range(3)]
        page2 = [_make_job(f"j2_{i}") for i in range(3)]
        responses = [
            _make_response(page1, next_cursor="cursor_page2"),
            _make_response(page2, next_cursor=None),
        ]

        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()

        assert len(result.jobs) == 6
        assert result.errors == []

    def test_raw_dict_keys_are_normalizer_compatible(self):
        jobs = [_make_job("k001")]
        responses = [_make_response(jobs)]

        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()

        raw = result.jobs[0]
        assert "source_job_id" in raw
        assert "title" in raw
        assert "company" in raw
        assert "description" in raw
        assert "location" in raw
        assert "countries" in raw
        assert "canonical_url" in raw

    def test_result_duration_is_set(self):
        responses = [_make_response([_make_job("k001")])]
        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()
        assert result.duration_seconds >= 0


# ---------------------------------------------------------------------------
# Stopping conditions
# ---------------------------------------------------------------------------


class TestStoppingConditions:
    def test_stops_when_no_next_cursor(self):
        """Should stop after page 1 when there's no cursor."""
        page1 = [_make_job(f"j{i}") for i in range(5)]
        mock = _mock_post([_make_response(page1, next_cursor=None)])

        with patch("httpx.post", mock):
            result = _collector({"max_pages_per_query": 10}).collect()

        assert len(result.jobs) == 5
        assert mock.call_count == 1

    def test_stops_at_max_pages(self):
        """Should stop at max_pages_per_query even when cursor is present."""
        def make_page(keys, cur):
            return _make_response([_make_job(k) for k in keys], next_cursor=cur)

        responses = [
            make_page([f"a{i}" for i in range(3)], "cur2"),
            make_page([f"b{i}" for i in range(3)], "cur3"),
            make_page([f"c{i}" for i in range(3)], "cur4"),  # page 3 = max
        ]
        mock = _mock_post(responses)

        with patch("httpx.post", mock):
            result = _collector({"max_pages_per_query": 3}).collect()

        assert mock.call_count == 3
        assert len(result.jobs) == 9

    def test_stops_on_empty_page(self):
        """Should stop immediately when results list is empty."""
        responses = [_make_response([], next_cursor="cursor")]
        mock = _mock_post(responses)

        with patch("httpx.post", mock):
            result = _collector().collect()

        assert mock.call_count == 1
        assert len(result.jobs) == 0

    def test_stops_when_all_keys_are_duplicates(self):
        """Should stop when every key on the page is already in seen_keys."""
        jobs = [_make_job(f"dup{i}") for i in range(3)]
        # Page 1: new keys; page 2: same keys (duplicates).
        responses = [
            _make_response(jobs, next_cursor="cur2"),
            _make_response(jobs, next_cursor="cur3"),
        ]
        mock = _mock_post(responses)

        with patch("httpx.post", mock):
            result = _collector({"max_pages_per_query": 10}).collect()

        assert mock.call_count == 2
        assert len(result.jobs) == 3  # only the first page's unique jobs

    def test_stops_when_max_jobs_per_query_reached(self):
        """Should stop collecting once max_jobs_per_query is hit."""
        # Two pages with 5 jobs each, but limit is 4.
        page1 = [_make_job(f"p1_{i}") for i in range(5)]
        page2 = [_make_job(f"p2_{i}") for i in range(5)]
        responses = [
            _make_response(page1, next_cursor="cur2"),
            _make_response(page2, next_cursor=None),
        ]
        mock = _mock_post(responses)

        with patch("httpx.post", mock):
            result = _collector({
                "max_jobs_per_query": 4,
                "max_pages_per_query": 10,
            }).collect()

        # First page delivers 5 > limit of 4, so the check fires BEFORE page 2
        assert mock.call_count == 1
        assert len(result.jobs) == 5  # page 1 was already processed


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_api_key_rejected_returns_error(self):
        mock = MagicMock(spec=httpx.Response)
        mock.status_code = 403
        mock.is_success = False

        with patch("httpx.post", return_value=mock):
            result = _collector().collect()

        assert result.jobs == []
        assert any("API key rejected" in e or "403" in e for e in result.errors)

    def test_http_400_returns_error(self):
        mock = MagicMock(spec=httpx.Response)
        mock.status_code = 400
        mock.is_success = False
        mock.text = "Bad request"

        with patch("httpx.post", return_value=mock):
            result = _collector().collect()

        assert result.jobs == []
        assert any("400" in e for e in result.errors)

    def test_graphql_errors_in_body_returns_error(self):
        responses = [{"errors": [{"message": "Something went wrong"}]}]
        mock = _mock_post(responses)

        with patch("httpx.post", mock):
            result = _collector().collect()

        assert result.jobs == []
        assert any("GraphQL" in e for e in result.errors)

    def test_network_timeout_returns_error(self):
        with patch("httpx.post", side_effect=httpx.TimeoutException("timed out")):
            result = _collector({"retries": 1}).collect()

        assert result.jobs == []
        assert len(result.errors) > 0

    def test_json_parse_error_returns_error(self):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.side_effect = ValueError("invalid JSON")

        with patch("httpx.post", return_value=mock_resp):
            result = _collector({"retries": 1}).collect()

        assert result.jobs == []
        assert len(result.errors) > 0

    def test_no_queries_configured_returns_error(self):
        collector = IndeedCollector({"queries": [], "discovery_queries": []})
        result = collector.collect()

        assert result.jobs == []
        assert any("No queries" in e for e in result.errors)

    def test_partial_success_continues_to_next_query(self):
        """When query 1 fails (403) and query 2 succeeds, we get query 2's jobs."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            mock_resp = MagicMock(spec=httpx.Response)
            if call_count == 0:
                mock_resp.status_code = 403
                mock_resp.is_success = False
            else:
                mock_resp.status_code = 200
                mock_resp.is_success = True
                mock_resp.json.return_value = _make_response([_make_job("q2_001")])
            call_count += 1
            return mock_resp

        collector = IndeedCollector({
            "queries": [
                {"keyword": "failing_query", "location": "France"},
                {"keyword": "python", "location": "France"},
            ],
            "rate_limit_delay": 0,
        })

        with patch("httpx.post", side_effect=side_effect):
            result = collector.collect()

        # Query 2 should have succeeded with 1 job.
        assert len(result.jobs) == 1
        assert len(result.errors) >= 1  # query 1's error


# ---------------------------------------------------------------------------
# Discovery queries
# ---------------------------------------------------------------------------


class TestDiscoveryQueries:
    def test_discovery_queries_run_against_default_location(self):
        """Keywords from discovery_queries use default_location."""
        recorded_payloads: list[dict] = []

        def capture_post(url, json, **kwargs):
            recorded_payloads.append(json)
            mock_resp = MagicMock(spec=httpx.Response)
            mock_resp.status_code = 200
            mock_resp.is_success = True
            mock_resp.json.return_value = _make_response([])
            return mock_resp

        collector = IndeedCollector({
            "discovery_queries": ["react", "node.js"],
            "default_location": "France",
            "rate_limit_delay": 0,
        })

        with patch("httpx.post", side_effect=capture_post):
            collector.collect()

        assert len(recorded_payloads) == 2
        assert all('where: "France"' in p["query"] for p in recorded_payloads)
        queries_combined = " ".join(p["query"] for p in recorded_payloads)
        assert "react" in queries_combined
        assert "node.js" in queries_combined

    def test_explicit_queries_and_discovery_queries_both_run(self):
        call_count = 0

        def count_calls(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock(spec=httpx.Response)
            mock_resp.status_code = 200
            mock_resp.is_success = True
            mock_resp.json.return_value = _make_response([])
            return mock_resp

        collector = IndeedCollector({
            "queries": [{"keyword": "python", "location": "France"}],
            "discovery_queries": ["java"],
            "rate_limit_delay": 0,
        })

        with patch("httpx.post", side_effect=count_calls):
            collector.collect()

        assert call_count == 2  # one per query


# ---------------------------------------------------------------------------
# Deduplication across queries
# ---------------------------------------------------------------------------


class TestCrossQueryDeduplication:
    def test_same_key_from_two_queries_counted_once(self):
        shared_job = _make_job("shared_key_001")
        responses = [
            _make_response([shared_job], next_cursor=None),
            _make_response([shared_job], next_cursor=None),
        ]
        mock = _mock_post(responses)

        collector = IndeedCollector({
            "queries": [
                {"keyword": "python", "location": "France"},
                {"keyword": "django", "location": "France"},
            ],
            "rate_limit_delay": 0,
        })

        with patch("httpx.post", mock):
            result = collector.collect()

        assert len(result.jobs) == 1


# ---------------------------------------------------------------------------
# CollectorResult metadata
# ---------------------------------------------------------------------------


class TestCollectorResult:
    def test_source_name_is_indeed(self):
        responses = [_make_response([])]
        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()
        assert result.source_name == "indeed"

    def test_jobs_are_dicts(self):
        jobs = [_make_job(f"j{i}") for i in range(3)]
        responses = [_make_response(jobs)]
        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()
        assert all(isinstance(j, dict) for j in result.jobs)

    def test_no_job_instances_in_result(self):
        """Collector must not construct Job model instances."""
        from src.models.job import Job
        jobs = [_make_job("k1")]
        responses = [_make_response(jobs)]
        with patch("httpx.post", _mock_post(responses)):
            result = _collector().collect()
        assert not any(isinstance(j, Job) for j in result.jobs)
