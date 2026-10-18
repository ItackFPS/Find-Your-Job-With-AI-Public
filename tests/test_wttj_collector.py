"""Unit tests for the WTTJ Crawl4AI-based collector.

All tests are offline — no network requests are made.  Crawl4AI is mocked via
sys.modules so the test suite runs even when crawl4ai is not installed.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers: fake crawl4ai module and fake crawler results
# ---------------------------------------------------------------------------

class _FakeCrawlResult:
    """Minimal stand-in for crawl4ai.AsyncCrawlResponse."""

    def __init__(
        self,
        success: bool = True,
        html: str = "",
        error_message: str = "",
        status_code: int = 200,
    ) -> None:
        self.success = success
        self.html = html
        self.error_message = error_message
        self.status_code = status_code


class _FakeAsyncWebCrawler:
    """Async context manager that delegates arun() to a configurable callback."""

    def __init__(self, config: Any = None) -> None:
        self.arun = AsyncMock()  # tests override .arun.return_value / side_effect

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


def _make_fake_crawl4ai() -> types.ModuleType:
    """Return a fake crawl4ai module suitable for sys.modules injection."""
    mod = types.ModuleType("crawl4ai")
    mod.AsyncWebCrawler = _FakeAsyncWebCrawler  # type: ignore[attr-defined]
    mod.BrowserConfig = MagicMock  # type: ignore[attr-defined]
    mod.CrawlerRunConfig = MagicMock  # type: ignore[attr-defined]

    cache_mode = MagicMock()
    cache_mode.BYPASS = "bypass"
    mod.CacheMode = cache_mode  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

LISTING_HTML = """
<html><body>
<a href="/fr/companies/acme/jobs/senior-engineer_paris">Senior Engineer</a>
<a href="/fr/companies/acme/jobs/product-manager_remote">Product Manager</a>
<a href="/fr/companies/acme/jobs/senior-engineer_paris">Senior Engineer</a>
</body></html>
"""

JOB_DETAIL_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "title": "Senior Engineer",
  "datePosted": "2026-08-01T10:00:00Z",
  "description": "<p>Great job with télétravail possible.</p>",
  "employmentType": "FULL_TIME",
  "hiringOrganization": {
    "@type": "Organization",
    "name": "ACME Corp"
  },
  "jobLocation": [{
    "@type": "Place",
    "address": {
      "@type": "PostalAddress",
      "addressLocality": "Paris",
      "addressRegion": "Île-de-France",
      "addressCountry": "FR",
      "postalCode": "75001"
    }
  }]
}
</script>
</head><body></body></html>
"""

JOB_DETAIL_REMOTE_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "title": "Remote Data Engineer",
  "datePosted": "2026-08-15T08:00:00Z",
  "description": "This is a 100% remote position.",
  "employmentType": "FULL_TIME",
  "hiringOrganization": {"@type": "Organization", "name": "Remote Co"},
  "jobLocation": [{
    "@type": "Place",
    "address": {
      "@type": "PostalAddress",
      "addressLocality": "Remote",
      "addressCountry": "FR",
      "postalCode": "75000"
    }
  }]
}
</script>
</head><body></body></html>
"""

JOB_DETAIL_MISSING_FIELDS_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "title": "Mystery Job"
}
</script>
</head><body></body></html>
"""

MALFORMED_HTML = """
<html><head>
<script type="application/ld+json">{ INVALID JSON }</script>
<script type="application/ld+json">{"@type": "WebPage"}</script>
</head><body></body></html>
"""

MULTI_LD_HTML = """
<html><head>
<script type="application/ld+json">{"@type": "FAQPage", "mainEntity": []}</script>
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "title": "CTO",
  "description": "Lead engineering.",
  "employmentType": "FULL_TIME",
  "hiringOrganization": {"name": "StartupX"},
  "jobLocation": [{"address": {"addressLocality": "Lyon", "addressCountry": "FR", "postalCode": "69001"}}]
}
</script>
</head><body></body></html>
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def inject_fake_crawl4ai():
    """Inject a fake crawl4ai module before each test and clean up after."""
    fake = _make_fake_crawl4ai()
    sys.modules["crawl4ai"] = fake
    yield fake
    sys.modules.pop("crawl4ai", None)


@pytest.fixture()
def crawler(inject_fake_crawl4ai: types.ModuleType) -> _FakeAsyncWebCrawler:
    """A pre-instantiated fake crawler (used to configure arun responses)."""
    c = _FakeAsyncWebCrawler()
    inject_fake_crawl4ai.AsyncWebCrawler = lambda config=None: c
    return c


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no async, no crawl4ai)
# ---------------------------------------------------------------------------

from src.collectors.wttj import (
    _detect_work_mode,
    _extract_job_posting_ld,
    _extract_job_urls_from_html,
    _map_ld_to_raw,
)


class TestExtractJobUrls:
    def test_basic_extraction(self) -> None:
        urls = _extract_job_urls_from_html(LISTING_HTML)
        assert len(urls) == 2
        assert "https://www.welcometothejungle.com/fr/companies/acme/jobs/senior-engineer_paris" in urls

    def test_deduplication(self) -> None:
        urls = _extract_job_urls_from_html(LISTING_HTML)
        assert len(urls) == len(set(urls))

    def test_empty_html(self) -> None:
        assert _extract_job_urls_from_html("") == []

    def test_no_job_links(self) -> None:
        assert _extract_job_urls_from_html("<html><body>no links</body></html>") == []

    def test_strips_query_params(self) -> None:
        html = '<a href="/fr/companies/x/jobs/slug?ref=search">job</a>'
        urls = _extract_job_urls_from_html(html)
        assert all("?" not in u for u in urls)


class TestExtractJsonLd:
    def test_extracts_job_posting(self) -> None:
        ld = _extract_job_posting_ld(JOB_DETAIL_HTML)
        assert ld is not None
        assert ld["@type"] == "JobPosting"
        assert ld["title"] == "Senior Engineer"

    def test_skips_non_job_posting(self) -> None:
        ld = _extract_job_posting_ld("<html><script type='application/ld+json'>{\"@type\": \"WebPage\"}</script></html>")
        assert ld is None

    def test_handles_malformed_json(self) -> None:
        ld = _extract_job_posting_ld(MALFORMED_HTML)
        assert ld is None

    def test_picks_job_posting_from_multiple_blocks(self) -> None:
        ld = _extract_job_posting_ld(MULTI_LD_HTML)
        assert ld is not None
        assert ld["title"] == "CTO"

    def test_empty_html(self) -> None:
        assert _extract_job_posting_ld("") is None


class TestMapLdToRaw:
    def test_full_mapping(self) -> None:
        ld = {
            "@type": "JobPosting",
            "title": "Senior Engineer",
            "datePosted": "2026-08-01T10:00:00Z",
            "description": "<p>Work with télétravail possible.</p>",
            "employmentType": "FULL_TIME",
            "hiringOrganization": {"name": "ACME Corp"},
            "jobLocation": [{
                "address": {
                    "addressLocality": "Paris",
                    "addressRegion": "Île-de-France",
                    "addressCountry": "FR",
                    "postalCode": "75001",
                }
            }],
        }
        url = "https://www.welcometothejungle.com/fr/companies/acme/jobs/senior-engineer_paris"
        raw = _map_ld_to_raw(ld, url)

        assert raw["title"] == "Senior Engineer"
        assert raw["company"] == "ACME Corp"
        assert raw["countries"] == ["FR"]
        assert raw["department_code"] == "75"
        assert raw["type"] == "full-time"
        assert raw["published_at"] == "2026-08-01T10:00:00Z"
        assert raw["canonical_url"] == url
        assert raw["source_job_id"] == "senior-engineer_paris"
        assert raw["work_mode"] == "hybrid"
        assert raw["remote"] is False

    def test_missing_optional_fields(self) -> None:
        ld = {"@type": "JobPosting", "title": "Mystery Job"}
        raw = _map_ld_to_raw(ld, "https://www.welcometothejungle.com/fr/companies/x/jobs/mystery")
        assert raw["title"] == "Mystery Job"
        assert raw["company"] is None
        assert raw["location"] == ""
        assert raw["countries"] == []
        assert raw["department_code"] is None
        assert raw["type"] is None
        assert raw["published_at"] is None
        assert raw["work_mode"] is None
        assert raw["remote"] is False

    def test_overseas_department_code(self) -> None:
        ld = {
            "@type": "JobPosting",
            "title": "Engineer",
            "jobLocation": [{"address": {"addressCountry": "FR", "postalCode": "97100"}}],
        }
        raw = _map_ld_to_raw(ld, "https://www.welcometothejungle.com/fr/companies/x/jobs/y")
        assert raw["department_code"] == "971"

    def test_non_fr_country_no_department(self) -> None:
        ld = {
            "@type": "JobPosting",
            "title": "Engineer",
            "jobLocation": [{"address": {"addressCountry": "DE", "postalCode": "10115"}}],
        }
        raw = _map_ld_to_raw(ld, "https://www.welcometothejungle.com/fr/companies/x/jobs/y")
        assert raw["department_code"] is None
        assert raw["countries"] == ["DE"]


class TestDetectWorkMode:
    def test_full_remote(self) -> None:
        assert _detect_work_mode("This is a 100% remote position.") == "remote"
        assert _detect_work_mode("Fully remote team.") == "remote"
        assert _detect_work_mode("100% télétravail.") == "remote"

    def test_hybrid(self) -> None:
        assert _detect_work_mode("Le télétravail est autorisé.") == "hybrid"
        assert _detect_work_mode("Hybrid work policy.") == "hybrid"
        assert _detect_work_mode("Some remote possible.") == "hybrid"

    def test_no_signal(self) -> None:
        assert _detect_work_mode("On-site role in Paris.") is None
        assert _detect_work_mode("") is None
        assert _detect_work_mode("Great benefits.") is None

    def test_full_remote_takes_priority(self) -> None:
        # Both signals present — full remote wins
        assert _detect_work_mode("100% remote with télétravail") == "remote"


# ---------------------------------------------------------------------------
# WTTJCollector integration tests (mocked crawl4ai)
# ---------------------------------------------------------------------------

from src.collectors.wttj import WTTJCollector


class TestWTTJCollectorSuccess:
    def test_extracts_jobs_from_single_company(self, crawler: _FakeAsyncWebCrawler) -> None:
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=LISTING_HTML),
            _FakeCrawlResult(success=True, html=JOB_DETAIL_HTML),
            _FakeCrawlResult(success=True, html=JOB_DETAIL_REMOTE_HTML),
        ]
        col = WTTJCollector(config={"company_slugs": ["acme"], "rate_limit_delay": 0})
        result = col.collect()

        assert result.source_name == "wttj"
        assert len(result.jobs) == 2
        assert result.errors == []

    def test_job_fields_are_correctly_mapped(self, crawler: _FakeAsyncWebCrawler) -> None:
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=LISTING_HTML),
            _FakeCrawlResult(success=True, html=JOB_DETAIL_HTML),
            _FakeCrawlResult(success=True, html="<html></html>"),  # second job: no LD
        ]
        col = WTTJCollector(config={"company_slugs": ["acme"], "rate_limit_delay": 0})
        result = col.collect()

        job = result.jobs[0]
        assert job["title"] == "Senior Engineer"
        assert job["company"] == "ACME Corp"
        assert job["type"] == "full-time"
        assert job["countries"] == ["FR"]
        assert job["department_code"] == "75"
        assert job["work_mode"] == "hybrid"

    def test_remote_work_mode_detected(self, crawler: _FakeAsyncWebCrawler) -> None:
        listing = '<a href="/fr/companies/acme/jobs/remote-job_remote">job</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            _FakeCrawlResult(success=True, html=JOB_DETAIL_REMOTE_HTML),
        ]
        col = WTTJCollector(config={"company_slugs": ["acme"], "rate_limit_delay": 0})
        result = col.collect()

        assert len(result.jobs) == 1
        assert result.jobs[0]["work_mode"] == "remote"
        assert result.jobs[0]["remote"] is True

    def test_multiple_companies(self, crawler: _FakeAsyncWebCrawler) -> None:
        single_link = '<a href="/fr/companies/x/jobs/eng_paris">eng</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=single_link),   # company A listing
            _FakeCrawlResult(success=True, html=JOB_DETAIL_HTML),  # company A job
            _FakeCrawlResult(success=True, html=single_link),   # company B listing
            _FakeCrawlResult(success=True, html=JOB_DETAIL_HTML),  # company B job
        ]
        col = WTTJCollector(config={"company_slugs": ["a", "b"], "rate_limit_delay": 0})
        result = col.collect()

        assert len(result.jobs) == 2
        assert result.errors == []

    def test_max_jobs_per_company_respected(self, crawler: _FakeAsyncWebCrawler) -> None:
        # Listing returns 2 links; max_jobs_per_company = 1
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=LISTING_HTML),   # 2 links
            _FakeCrawlResult(success=True, html=JOB_DETAIL_HTML), # only 1 visited
        ]
        col = WTTJCollector(config={
            "company_slugs": ["acme"],
            "max_jobs_per_company": 1,
            "rate_limit_delay": 0,
        })
        result = col.collect()

        assert len(result.jobs) == 1

    def test_json_ld_in_multiple_script_blocks(self, crawler: _FakeAsyncWebCrawler) -> None:
        listing = '<a href="/fr/companies/x/jobs/cto_lyon">cto</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            _FakeCrawlResult(success=True, html=MULTI_LD_HTML),
        ]
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        assert len(result.jobs) == 1
        assert result.jobs[0]["title"] == "CTO"
        assert result.jobs[0]["department_code"] == "69"


class TestWTTJCollectorMissingFields:
    def test_missing_company_and_location_fields(self, crawler: _FakeAsyncWebCrawler) -> None:
        listing = '<a href="/fr/companies/x/jobs/mystery">mystery</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            _FakeCrawlResult(success=True, html=JOB_DETAIL_MISSING_FIELDS_HTML),
        ]
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Mystery Job"
        assert job["company"] is None
        assert job["location"] == ""
        assert job["department_code"] is None
        assert result.errors == []


class TestWTTJCollectorErrors:
    def test_malformed_page_skipped_not_raised(self, crawler: _FakeAsyncWebCrawler) -> None:
        listing = '<a href="/fr/companies/x/jobs/bad">bad</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            _FakeCrawlResult(success=True, html=MALFORMED_HTML),
        ]
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        # No job extracted, but no exception raised and error is logged as warning
        assert result.jobs == []
        assert result.errors == []  # page-level "no JSON-LD" is a warning, not an error

    def test_network_failure_on_listing_page(self, crawler: _FakeAsyncWebCrawler) -> None:
        crawler.arun.return_value = _FakeCrawlResult(
            success=False, error_message="Connection refused", status_code=0
        )
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        assert result.jobs == []
        assert len(result.errors) == 1
        assert "Connection refused" in result.errors[0]

    def test_network_failure_on_detail_page_is_skipped(self, crawler: _FakeAsyncWebCrawler) -> None:
        listing = '<a href="/fr/companies/x/jobs/eng">eng</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            _FakeCrawlResult(success=False, error_message="Timeout", status_code=0),
        ]
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        # Detail page failure is recorded as an error but does not crash
        assert result.jobs == []
        assert any("Timeout" in e for e in result.errors)

    def test_no_company_slugs_configured(self) -> None:
        col = WTTJCollector(config={"company_slugs": [], "rate_limit_delay": 0})
        result = col.collect()

        assert result.jobs == []
        assert result.errors != []
        assert "No company_slugs" in result.errors[0]

    def test_exception_in_arun_is_caught(self, crawler: _FakeAsyncWebCrawler) -> None:
        listing = '<a href="/fr/companies/x/jobs/eng">eng</a>'
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            RuntimeError("Browser crashed"),
        ]
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        assert result.jobs == []
        assert any("Browser crashed" in e for e in result.errors)


class TestWTTJCollectorDuplicates:
    def test_same_url_twice_in_listing_yields_one_job(self, crawler: _FakeAsyncWebCrawler) -> None:
        # Listing has the same link twice — _extract_job_urls_from_html deduplicates
        listing = """
        <a href="/fr/companies/x/jobs/eng_paris">eng</a>
        <a href="/fr/companies/x/jobs/eng_paris">eng duplicate</a>
        """
        crawler.arun.side_effect = [
            _FakeCrawlResult(success=True, html=listing),
            _FakeCrawlResult(success=True, html=JOB_DETAIL_HTML),
        ]
        col = WTTJCollector(config={"company_slugs": ["x"], "rate_limit_delay": 0})
        result = col.collect()

        assert len(result.jobs) == 1


class TestCrawl4AINotInstalled:
    def test_import_error_when_crawl4ai_missing(self) -> None:
        # Setting sys.modules["crawl4ai"] = None tells Python the module does
        # not exist regardless of whether it is installed on the system.
        with patch.dict(sys.modules, {"crawl4ai": None}):
            col = WTTJCollector(config={"company_slugs": ["x"]})
            with pytest.raises(ImportError, match="crawl4ai"):
                col.collect()
