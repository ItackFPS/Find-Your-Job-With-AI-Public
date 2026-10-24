"""We Work Remotely (weworkremotely.com) collector.

Discovers remote jobs via the global RSS feed plus category-specific RSS feeds:

    https://weworkremotely.com/remote-jobs.rss               (global, ~89 recent)
    https://weworkremotely.com/categories/{slug}.rss          (per-category)

Both sources feed into a shared in-run deduplication set keyed on the numeric
job ID embedded in every WWR URL, so the same job is only emitted once per
run regardless of how many RSS feeds list it.

Why category RSS feeds instead of HTML scraping:
    As of 2026-08-28, all WWR HTML pages (including /categories/* and
    /remote-jobs) sit behind Cloudflare bot protection ("Just a moment…",
    HTTP 403 for plain httpx).  The RSS endpoints are publicly accessible
    without JavaScript challenges.  Category feeds overlap partially with the
    global feed but each has a different sliding window, so together they
    surface substantially more unique jobs (measured: 229 vs 89 RSS-only).

    Note: some category feeds also return HTTP 403 (e.g. /remote-python-jobs.rss
    as of 2026-08-28).  The collector treats each feed independently — a 403 on
    one feed is logged and skipped; others continue unaffected.

Discovery flow:
    global RSS feed  ──────────────────────────┐
                                               ├─► job ID dedup ─► emit raw dicts
    category RSS feeds (one per configured slug)┘

Configuration keys (config: sub-section in sources.yaml):
    careers_url         Global RSS URL (default: https://weworkremotely.com/remote-jobs.rss).
    category_rss        bool — fetch category-specific RSS feeds (default: true).
    categories          list — WWR category slugs to try as RSS feeds (default: see below).
    rate_limit_delay    float — seconds between feed requests (default: 1.5).
    timeout             int   — per-request timeout in seconds (default: 30).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import feedparser
import httpx

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_RSS_URL = "https://weworkremotely.com/remote-jobs.rss"
_CATEGORY_RSS_TEMPLATE = "https://weworkremotely.com/categories/{slug}.rss"

# Numeric ID embedded in all WWR job and feed URLs
_JOB_PATH_RE = re.compile(r"/remote-jobs/(?:view/)?(\d+)")

_RSS_HEADERS: dict[str, str] = {
    "Accept": "application/rss+xml, application/xml, text/xml",
    "User-Agent": "Mozilla/5.0 (compatible; job-aggregator/1.0)",
}

# Category slugs whose RSS feeds are verified accessible (HTTP 200).
# Some slugs (e.g. remote-python-jobs) return HTTP 403 as of 2026-08-28;
# they are tried anyway — the collector logs and skips 403s gracefully.
# Measured discovery per feed (2026-08-28):
#   remote-programming-jobs              →  25 entries, ~18 new unique
#   remote-full-stack-programming-jobs   → 119 entries, ~82 new unique  ★ high-value
#   remote-back-end-programming-jobs     →   5 entries,  ~0 new unique
#   remote-front-end-programming-jobs    →   4 entries,  ~0 new unique
#   remote-devops-sysadmin-jobs          →  12 entries,  ~2 new unique
#   remote-design-jobs                   →  63 entries, ~40 new unique
_DEFAULT_CATEGORIES: list[str] = [
    "remote-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-front-end-programming-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-design-jobs",
]


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class WeWorkRemotelyCollector(BaseCollector):
    """Collects remote jobs from We Work Remotely (weworkremotely.com).

    Combines the global RSS feed with category-specific RSS feeds to
    discover substantially more unique jobs than the global feed alone.
    """

    # public_page: public RSS feeds (global and per category). robots.txt (checked
    # 2026-10-24) allows / except account and admin paths; no terms grant reuse
    # explicitly, so the feeds are read as public pages with robots.txt checked live.
    access_class = "public_page"
    robots_policy = True
    robots_agent = "job-aggregator"  # product token of the User-Agent sent
    robots_user_agent = _RSS_HEADERS["User-Agent"]
    # 1 global + 6 category feeds per run by default.
    rate_policy = RatePolicy(min_delay_seconds=1.0, max_requests_per_run=100)

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("weworkremotely_feed.rss", self._load_fixture("weworkremotely_feed.rss"),
                               parse_feed)

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 30)),
            retries=3,
            rate_limit_delay=float(cfg.get("rate_limit_delay", 1.5)),
        )
        self.rss_url: str = cfg.get("careers_url") or _DEFAULT_RSS_URL
        self.category_rss: bool = bool(cfg.get("category_rss", True))
        self.categories: list[str] = list(cfg.get("categories") or _DEFAULT_CATEGORIES)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        # Shared dedup: numeric job IDs already emitted this run.
        # Global RSS fills it first; category feeds skip IDs already present.
        seen_job_ids: set[str] = set()

        # -- Phase 1: global RSS -------------------------------------------
        global_jobs, global_errors = self._fetch_rss_feed(
            self.rss_url, label="global", seen_job_ids=seen_job_ids
        )
        jobs.extend(global_jobs)
        errors.extend(global_errors)

        # -- Phase 2: category RSS feeds -----------------------------------
        if self.category_rss:
            for slug in self.categories:
                url = _CATEGORY_RSS_TEMPLATE.format(slug=slug)
                cat_jobs, cat_errors = self._fetch_rss_feed(
                    url, label=slug, seen_job_ids=seen_job_ids
                )
                jobs.extend(cat_jobs)
                errors.extend(cat_errors)

        duration = time.time() - start_time
        self._log_collection("weworkremotely", len(jobs), errors, duration)
        return CollectorResult(
            source_name="weworkremotely",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
            metadata={
                "global_count": len(global_jobs),
                "category_count": len(jobs) - len(global_jobs),
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_rss_feed(
        self,
        url: str,
        label: str,
        seen_job_ids: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch one WWR RSS feed and return new jobs not yet in *seen_job_ids*.

        Non-fatal: network errors, HTTP 4xx/5xx, and parse errors are
        returned as error strings rather than raised.
        """
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        try:
            self._enforce_rate_limit()
            response = self._fetch_with_retry(url, headers=_RSS_HEADERS)
        except (httpx.HTTPError, RuntimeError) as exc:
            errors.append(f"WWR [{label}] fetch failed: {exc}")
            return jobs, errors

        try:
            feed = feedparser.parse(response.text)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"WWR [{label}] parse error: {exc}")
            return jobs, errors

        if getattr(feed, "bozo", False) and not feed.entries:
            errors.append(
                f"WWR [{label}] RSS malformed: {getattr(feed, 'bozo_exception', 'unknown')}"
            )
            return jobs, errors

        new_count = 0
        for entry in feed.entries:
            raw = _rss_entry_to_raw(entry)
            job_id = raw.get("source_job_id") or ""
            if job_id in seen_job_ids:
                continue  # already collected from the global or an earlier category feed
            if job_id:
                seen_job_ids.add(job_id)
            jobs.append(raw)
            new_count += 1

        logger.info(
            "WWR RSS [%s]: entries=%d new=%d url=%s",
            label, len(feed.entries), new_count, url,
        )
        return jobs, errors


# ---------------------------------------------------------------------------
# Pure helpers — no network, fully testable
# ---------------------------------------------------------------------------

def parse_feed(text: str) -> list[dict[str, Any]]:
    """The offers in one WWR RSS feed, as raw dicts (no cross-feed dedup)."""
    return [_rss_entry_to_raw(entry) for entry in feedparser.parse(text).entries]


def _extract_job_id(url_or_path: str) -> str:
    """Return the numeric job ID embedded in a WWR URL/path, or ''."""
    m = _JOB_PATH_RE.search(url_or_path)
    return m.group(1) if m else ""


def _canonical_url(url: str) -> str:
    """Strip fragment and tracking query params from a WWR job URL."""
    return url.split("#")[0].split("?")[0].rstrip("/")


def _rss_entry_to_raw(entry: Any) -> dict[str, Any]:
    """Convert a feedparser RSS entry from any WWR feed to a raw job dict.

    WWR RSS title format: "Company Name: Job Title"
    WWR RSS links include fragments: /remote-jobs/view/123-slug#job-123
    The numeric job ID is extracted from the link for stable deduplication.
    """
    raw_title = str(getattr(entry, "title", "") or "").strip()

    # Split "Company: Job Title" on the first ":" only
    company = ""
    title = raw_title
    if ":" in raw_title:
        maybe_company, _, maybe_title = raw_title.partition(":")
        if maybe_company.strip() and maybe_title.strip():
            company = maybe_company.strip()
            title = maybe_title.strip()

    description = str(
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
        or ""
    )
    link = str(getattr(entry, "link", "") or "")
    entry_id = str(getattr(entry, "id", "") or link)

    # Canonical URL: strip fragment so different feeds pointing to the same
    # job produce the same canonical URL.
    canonical = _canonical_url(link) if link else ""

    # Prefer the numeric job ID as the stable identifier; fall back to the
    # full entry ID (URL with fragment) when the path pattern is absent.
    job_id = _extract_job_id(link) or _extract_job_id(entry_id)

    categories: list[str] = []
    for tag in getattr(entry, "tags", []) or []:
        term = tag.get("term") if isinstance(tag, dict) else None
        if term:
            categories.append(str(term))

    return {
        "title": title,
        "company": company,
        "description": description,
        "location": "Remote",
        "remote": True,
        "url": canonical or link,
        "canonical_url": canonical or link,
        "source_job_id": job_id or entry_id,
        "categories": categories,
    }
