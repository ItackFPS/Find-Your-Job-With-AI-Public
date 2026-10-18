"""Comprehensive tests for the WeWorkRemotelyCollector.

All tests use fixtures and mocked HTTP — no live network calls.

Coverage:
  - Global RSS discovery (parsing, company/title split, URL normalization)
  - Category RSS feed discovery (additional jobs beyond the global window)
  - In-run deduplication (global + category find same job → emitted once)
  - Same job in multiple categories → emitted once
  - Per-feed error isolation: one broken feed doesn't stop the rest
  - category_rss=False skips category feeds
  - Canonical URL handling (fragments stripped, stable source_job_id)
  - Source_job_id is the numeric ID (not the full URL with fragment)
  - RSS feed HTTP 403 handled gracefully (logged, not raised)
  - Malformed RSS handled gracefully
  - Registry wires up the collector correctly
  - Compatibility with the pipeline normalizer (stable IDs, normalization)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import feedparser
import httpx

from src.collectors.weworkremotely import (
    WeWorkRemotelyCollector,
    _canonical_url,
    _extract_job_id,
    _rss_entry_to_raw,
)

# ---------------------------------------------------------------------------
# RSS feed fixtures
# ---------------------------------------------------------------------------

GLOBAL_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>We Work Remotely: Remote Jobs</title>
    <link>https://weworkremotely.com</link>
    <item>
      <title>Acme Corp: Senior Backend Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer#job-11111</link>
      <guid>https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer#job-11111</guid>
      <description>&lt;p&gt;We are looking for a backend engineer skilled in Python.&lt;/p&gt;</description>
      <category>Programming</category>
      <category>Back-End</category>
    </item>
    <item>
      <title>Beta Inc: JavaScript Developer</title>
      <link>https://weworkremotely.com/remote-jobs/view/22222-javascript-developer#job-22222</link>
      <guid>https://weworkremotely.com/remote-jobs/view/22222-javascript-developer#job-22222</guid>
      <description>&lt;p&gt;Build our React frontend.&lt;/p&gt;</description>
      <category>JavaScript</category>
    </item>
    <item>
      <title>Listing Without A Colon Separator</title>
      <link>https://weworkremotely.com/remote-jobs/view/33333-some-role#job-33333</link>
      <guid>https://weworkremotely.com/remote-jobs/view/33333-some-role#job-33333</guid>
      <description>A remote role.</description>
    </item>
  </channel>
</rss>"""

# Category feed: introduces 2 new jobs (44444, 55555) and repeats 11111
CATEGORY_RSS_PROGRAMMING = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>We Work Remotely: Remote Programming Jobs</title>
    <link>https://weworkremotely.com</link>
    <item>
      <title>Acme Corp: Senior Backend Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer#job-11111</link>
      <guid>https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer#job-11111</guid>
      <description>&lt;p&gt;Python backend role.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Delta LLC: Platform Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/view/44444-platform-engineer#job-44444</link>
      <guid>https://weworkremotely.com/remote-jobs/view/44444-platform-engineer#job-44444</guid>
      <description>&lt;p&gt;Build infrastructure at scale.&lt;/p&gt;</description>
      <category>DevOps</category>
    </item>
    <item>
      <title>Epsilon Co: Automation Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/view/55555-automation-engineer#job-55555</link>
      <guid>https://weworkremotely.com/remote-jobs/view/55555-automation-engineer#job-55555</guid>
      <description>&lt;p&gt;Test automation with Python and Selenium.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>"""

# A second category feed: introduces job 66666, repeats 44444
CATEGORY_RSS_FULLSTACK = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>We Work Remotely: Remote Full-Stack Jobs</title>
    <link>https://weworkremotely.com</link>
    <item>
      <title>Delta LLC: Platform Engineer</title>
      <link>https://weworkremotely.com/remote-jobs/view/44444-platform-engineer#job-44444</link>
      <guid>https://weworkremotely.com/remote-jobs/view/44444-platform-engineer#job-44444</guid>
      <description>&lt;p&gt;Build infrastructure.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Zeta Corp: Staff SRE</title>
      <link>https://weworkremotely.com/remote-jobs/view/66666-staff-sre#job-66666</link>
      <guid>https://weworkremotely.com/remote-jobs/view/66666-staff-sre#job-66666</guid>
      <description>&lt;p&gt;Lead SRE work.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>"""

MALFORMED_RSS = "this is not xml at all {{{{{"

EMPTY_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title></channel></rss>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(text: str = "", status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.headers = {}
    return resp


def _mock_403() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 403
    resp.text = "Forbidden"
    exc = httpx.HTTPStatusError("403", request=MagicMock(), response=resp)
    resp.raise_for_status.side_effect = exc
    return resp


def _make_collector(**cfg_overrides: Any) -> WeWorkRemotelyCollector:
    base_cfg: dict[str, Any] = {
        "careers_url": "https://weworkremotely.com/remote-jobs.rss",
        "category_rss": False,  # disabled by default in tests — opt in
        "rate_limit_delay": 0.0,
        "timeout": 10,
    }
    base_cfg.update(cfg_overrides)
    return WeWorkRemotelyCollector(config=base_cfg)


# ===========================================================================
# Unit tests — pure helpers (no network)
# ===========================================================================

class TestExtractJobId:
    def test_from_full_url(self) -> None:
        assert _extract_job_id("https://weworkremotely.com/remote-jobs/view/12345-slug") == "12345"

    def test_from_path(self) -> None:
        assert _extract_job_id("/remote-jobs/view/99999-senior-engineer") == "99999"

    def test_from_url_with_fragment(self) -> None:
        assert _extract_job_id("https://weworkremotely.com/remote-jobs/view/12345-slug#job-12345") == "12345"

    def test_returns_empty_for_non_job_url(self) -> None:
        assert _extract_job_id("https://weworkremotely.com/categories/remote-programming-jobs") == ""

    def test_returns_empty_for_empty_string(self) -> None:
        assert _extract_job_id("") == ""


class TestCanonicalUrl:
    def test_strips_fragment(self) -> None:
        url = "https://weworkremotely.com/remote-jobs/view/12345-slug#job-12345"
        assert _canonical_url(url) == "https://weworkremotely.com/remote-jobs/view/12345-slug"

    def test_strips_query_params(self) -> None:
        url = "https://weworkremotely.com/remote-jobs/view/12345-slug?ref=twitter"
        assert _canonical_url(url) == "https://weworkremotely.com/remote-jobs/view/12345-slug"

    def test_strips_both(self) -> None:
        url = "https://weworkremotely.com/remote-jobs/view/12345-slug?ref=rss#job-12345"
        assert _canonical_url(url) == "https://weworkremotely.com/remote-jobs/view/12345-slug"

    def test_passthrough_for_clean_url(self) -> None:
        url = "https://weworkremotely.com/remote-jobs/view/12345-slug"
        assert _canonical_url(url) == url


class TestRssEntryToRaw:
    """Tests for _rss_entry_to_raw() using real feedparser entries."""

    def _first_entry(self, rss: str) -> Any:
        return feedparser.parse(rss).entries[0]

    def test_splits_company_and_title(self) -> None:
        entry = self._first_entry(GLOBAL_RSS)
        raw = _rss_entry_to_raw(entry)
        assert raw["company"] == "Acme Corp"
        assert raw["title"] == "Senior Backend Engineer"

    def test_no_colon_means_full_title_no_company(self) -> None:
        entry = feedparser.parse(GLOBAL_RSS).entries[2]
        raw = _rss_entry_to_raw(entry)
        assert raw["title"] == "Listing Without A Colon Separator"
        assert raw["company"] == ""

    def test_canonical_url_has_no_fragment(self) -> None:
        entry = self._first_entry(GLOBAL_RSS)
        raw = _rss_entry_to_raw(entry)
        assert "#" not in raw["canonical_url"]
        assert raw["canonical_url"] == "https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer"

    def test_source_job_id_is_numeric_id(self) -> None:
        entry = self._first_entry(GLOBAL_RSS)
        raw = _rss_entry_to_raw(entry)
        assert raw["source_job_id"] == "11111"

    def test_remote_flag_is_true(self) -> None:
        entry = self._first_entry(GLOBAL_RSS)
        assert _rss_entry_to_raw(entry)["remote"] is True

    def test_categories_parsed(self) -> None:
        entry = self._first_entry(GLOBAL_RSS)
        raw = _rss_entry_to_raw(entry)
        assert "Programming" in raw["categories"]
        assert "Back-End" in raw["categories"]

    def test_description_present(self) -> None:
        entry = self._first_entry(GLOBAL_RSS)
        raw = _rss_entry_to_raw(entry)
        assert "backend engineer" in raw["description"].lower()


# ===========================================================================
# Integration tests — collector with mocked HTTP
# ===========================================================================

class TestGlobalRssOnly:
    """WeWorkRemotelyCollector with category_rss=False."""

    def test_fetches_configured_rss_url(self) -> None:
        collector = _make_collector()

        with patch.object(
            collector, "_fetch_with_retry", return_value=_mock_response(GLOBAL_RSS)
        ) as mock_fetch:
            result = collector.collect()

        mock_fetch.assert_called_once()
        called_url = mock_fetch.call_args.args[0]
        assert "weworkremotely.com" in called_url
        assert result.errors == []

    def test_returns_correct_job_count(self) -> None:
        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(GLOBAL_RSS)):
            result = collector.collect()

        assert len(result.jobs) == 3

    def test_job_fields_correct(self) -> None:
        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(GLOBAL_RSS)):
            result = collector.collect()

        first = result.jobs[0]
        assert first["company"] == "Acme Corp"
        assert first["title"] == "Senior Backend Engineer"
        assert first["remote"] is True
        assert "#" not in first.get("canonical_url", "")

    def test_rss_failure_returns_error_not_exception(self) -> None:
        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", side_effect=RuntimeError("timeout")):
            result = collector.collect()

        assert result.jobs == []
        assert any("global" in e.lower() or "RSS" in e or "fetch" in e.lower() for e in result.errors)

    def test_malformed_rss_returns_empty_not_exception(self) -> None:
        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(MALFORMED_RSS)):
            result = collector.collect()

        # feedparser is lenient; must not raise either way
        assert isinstance(result.jobs, list)

    def test_empty_rss_feed_gives_no_jobs_no_errors(self) -> None:
        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(EMPTY_RSS)):
            result = collector.collect()

        assert result.jobs == []
        assert result.errors == []

    def test_category_feeds_skipped_when_category_rss_false(self) -> None:
        collector = _make_collector(category_rss=False)
        fetched: list[str] = []

        def side_effect(url: str, **_: Any) -> MagicMock:
            fetched.append(url)
            return _mock_response(GLOBAL_RSS)

        with patch.object(collector, "_fetch_with_retry", side_effect=side_effect):
            collector.collect()

        assert len(fetched) == 1  # only the global RSS

    def test_metadata_reports_counts(self) -> None:
        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(GLOBAL_RSS)):
            result = collector.collect()

        assert result.metadata.get("global_count") == 3
        assert result.metadata.get("category_count") == 0


class TestCategoryRssFeeds:
    """WeWorkRemotelyCollector with category_rss=True."""

    def _two_category_collector(self) -> WeWorkRemotelyCollector:
        return _make_collector(
            category_rss=True,
            categories=["remote-programming-jobs", "remote-full-stack-programming-jobs"],
        )

    def _dispatch(self, url: str, **_: Any) -> MagicMock:
        """Return different RSS fixtures based on the URL slug."""
        if "remote-jobs.rss" in url:
            return _mock_response(GLOBAL_RSS)
        if "programming-jobs.rss" in url and "full-stack" not in url:
            return _mock_response(CATEGORY_RSS_PROGRAMMING)
        if "full-stack" in url:
            return _mock_response(CATEGORY_RSS_FULLSTACK)
        return _mock_response(EMPTY_RSS)

    def test_category_feeds_are_fetched(self) -> None:
        collector = self._two_category_collector()
        fetched: list[str] = []

        def side_effect(url: str, **kw: Any) -> MagicMock:
            fetched.append(url)
            return self._dispatch(url)

        with patch.object(collector, "_fetch_with_retry", side_effect=side_effect):
            collector.collect()

        # global RSS + 2 category feeds = 3 total fetches
        assert len(fetched) == 3
        assert any("categories" in u for u in fetched)

    def test_category_feed_url_pattern(self) -> None:
        collector = _make_collector(
            category_rss=True,
            categories=["remote-programming-jobs"],
        )
        fetched: list[str] = []

        def side_effect(url: str, **_: Any) -> MagicMock:
            fetched.append(url)
            return _mock_response(GLOBAL_RSS)

        with patch.object(collector, "_fetch_with_retry", side_effect=side_effect):
            collector.collect()

        category_urls = [u for u in fetched if "categories" in u]
        assert len(category_urls) == 1
        assert category_urls[0] == "https://weworkremotely.com/categories/remote-programming-jobs.rss"

    def test_global_job_repeated_in_category_not_duplicated(self) -> None:
        """Job 11111 is in global RSS and programming category → appears once."""
        collector = self._two_category_collector()

        with patch.object(collector, "_fetch_with_retry", side_effect=self._dispatch):
            result = collector.collect()

        source_ids = [j.get("source_job_id") for j in result.jobs]
        assert source_ids.count("11111") == 1

    def test_same_job_in_two_categories_not_duplicated(self) -> None:
        """Job 44444 is in both programming and full-stack categories → appears once."""
        collector = self._two_category_collector()

        with patch.object(collector, "_fetch_with_retry", side_effect=self._dispatch):
            result = collector.collect()

        source_ids = [j.get("source_job_id") for j in result.jobs]
        assert source_ids.count("44444") == 1

    def test_total_unique_jobs_correct(self) -> None:
        """Global: 11111, 22222, 33333. Programming adds 44444, 55555. Full-stack adds 66666."""
        collector = self._two_category_collector()

        with patch.object(collector, "_fetch_with_retry", side_effect=self._dispatch):
            result = collector.collect()

        # 6 unique jobs: 11111, 22222, 33333, 44444, 55555, 66666
        assert len(result.jobs) == 6

    def test_metadata_category_count_correct(self) -> None:
        collector = self._two_category_collector()

        with patch.object(collector, "_fetch_with_retry", side_effect=self._dispatch):
            result = collector.collect()

        assert result.metadata["global_count"] == 3
        assert result.metadata["category_count"] == 3  # 44444, 55555, 66666

    def test_broken_category_feed_does_not_stop_others(self) -> None:
        """First category fails; second category still returns jobs."""
        collector = self._two_category_collector()

        def side_effect(url: str, **_: Any) -> MagicMock:
            if "remote-jobs.rss" in url:
                return _mock_response(GLOBAL_RSS)
            if "programming-jobs.rss" in url and "full-stack" not in url:
                raise RuntimeError("DNS timeout")
            return _mock_response(CATEGORY_RSS_FULLSTACK)

        with patch.object(collector, "_fetch_with_retry", side_effect=side_effect):
            result = collector.collect()

        # Error recorded for the broken feed
        assert any("programming" in e.lower() or "remote-programming" in e for e in result.errors)
        # But full-stack jobs still collected (44444, 66666)
        source_ids = {j.get("source_job_id") for j in result.jobs}
        assert "66666" in source_ids  # from full-stack
        assert len(result.jobs) > 3   # more than just global

    def test_403_on_category_feed_logged_and_skipped(self) -> None:
        """An HTTP 403 (Cloudflare) on a category feed is logged, not raised."""
        collector = _make_collector(
            category_rss=True,
            categories=["remote-python-jobs"],
        )

        def side_effect(url: str, **_: Any) -> MagicMock:
            if "remote-jobs.rss" in url:
                return _mock_response(GLOBAL_RSS)
            # Simulate Cloudflare 403 on the category feed
            raise RuntimeError("HTTP 403: Forbidden")

        with patch.object(collector, "_fetch_with_retry", side_effect=side_effect):
            result = collector.collect()

        # The global RSS jobs are still present
        assert len(result.jobs) == 3
        # An error is recorded for the failed category
        assert any("403" in e or "python" in e.lower() or "fetch" in e.lower() for e in result.errors)


class TestCollectorRegistration:
    def test_weworkremotely_registered_in_registry(self) -> None:
        from src.collectors.registry import CollectorRegistry
        from src.collectors.weworkremotely import WeWorkRemotelyCollector

        reg = CollectorRegistry()
        cls = reg.get_collector_class("weworkremotely")
        assert cls is WeWorkRemotelyCollector

    def test_rss_alias_still_registered_for_backward_compat(self) -> None:
        from src.collectors.registry import CollectorRegistry

        reg = CollectorRegistry()
        assert "rss" in reg.list_collectors()

    def test_collector_instantiated_via_registry(self) -> None:
        from src.collectors.registry import CollectorRegistry
        from src.collectors.weworkremotely import WeWorkRemotelyCollector

        reg = CollectorRegistry()
        cls = reg.get_collector_class("weworkremotely")
        instance = cls(config={"category_rss": False, "rate_limit_delay": 0.0})
        assert isinstance(instance, WeWorkRemotelyCollector)


class TestNormalizerCompatibility:
    """Verify raw dicts from the collector pass through the pipeline normalizer."""

    def test_rss_raw_dict_normalizes_without_error(self) -> None:
        from src.pipeline.normalize import normalize_job

        collector = _make_collector()

        with patch.object(collector, "_fetch_with_retry", return_value=_mock_response(GLOBAL_RSS)):
            result = collector.collect()

        for raw in result.jobs:
            job = normalize_job("weworkremotely", raw)
            assert job.source == "weworkremotely"
            assert job.title

    def test_stable_id_same_for_global_and_category_discovery(self) -> None:
        """Same numeric job ID from global RSS and a category feed → same stable DB id."""
        import hashlib
        from src.pipeline.normalize import normalize_job

        # Job 11111 appears in both global RSS and CATEGORY_RSS_PROGRAMMING
        # Both must produce the same stable id.

        # Simulate global RSS raw
        global_raw = {
            "title": "Senior Backend Engineer",
            "company": "Acme Corp",
            "description": "Python backend role.",
            "url": "https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer",
            "canonical_url": "https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer",
            "source_job_id": "11111",
            "remote": True,
            "location": "Remote",
            "categories": ["Programming"],
        }
        # Simulate category RSS raw (same job)
        category_raw = {
            "title": "Senior Backend Engineer",
            "company": "Acme Corp",
            "description": "Python backend role.",
            "url": "https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer",
            "canonical_url": "https://weworkremotely.com/remote-jobs/view/11111-senior-backend-engineer",
            "source_job_id": "11111",
            "remote": True,
            "location": "Remote",
            "categories": [],
        }

        global_job = normalize_job("weworkremotely", global_raw)
        category_job = normalize_job("weworkremotely", category_raw)

        expected_id = hashlib.sha256("weworkremotely:11111".encode()).hexdigest()[:32]
        assert global_job.id == expected_id
        assert category_job.id == expected_id

    def test_canonical_url_set_on_normalized_job(self) -> None:
        from src.pipeline.normalize import normalize_job

        raw = {
            "title": "Staff SRE",
            "company": "Zeta Corp",
            "description": "Lead SRE work.",
            "url": "https://weworkremotely.com/remote-jobs/view/66666-staff-sre",
            "canonical_url": "https://weworkremotely.com/remote-jobs/view/66666-staff-sre",
            "source_job_id": "66666",
            "remote": True,
            "location": "Remote",
            "categories": [],
        }
        job = normalize_job("weworkremotely", raw)
        assert job.canonical_url == "https://weworkremotely.com/remote-jobs/view/66666-staff-sre"
        assert job.source_job_id == "66666"
