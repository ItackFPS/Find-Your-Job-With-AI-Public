"""Welcome to the Jungle (WTTJ) browser-based collector.

Uses Crawl4AI (Playwright) to render company job listing pages and individual
job detail pages, then extracts structured data via schema.org/JobPosting
JSON-LD — no LLM required.

Robots.txt compliance (verified 2026-08-27):
  Allowed:  /fr/companies/{slug}/jobs        (company listing, no query params)
  Allowed:  /fr/companies/{slug}/jobs/{slug} (job detail, no query params)
  Blocked:  any URL containing ?             (Disallow: /*?)

Discovery strategy:
  1. For each configured company_slug, load the company jobs listing page.
  2. Extract all clean job-detail URLs from the rendered HTML.
  3. For each job-detail URL (up to max_jobs_per_company), render the page
     and parse the first schema.org/JobPosting JSON-LD block.
  4. Map JSON-LD fields to the normalizer raw-dict contract and return.

LLM extraction is intentionally not used.  If a future source requires
semantic extraction, plug in the local model at that point rather than
adding a cloud provider.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from src.collectors.base import CollectorResult
from src.collectors.base_browser import BaseBrowserCollector

logger = logging.getLogger(__name__)

class _DetailPageError(RuntimeError):
    """Raised when a job detail page fails to load (network or HTTP error)."""


_BASE_URL = "https://www.welcometothejungle.com"
_COMPANY_JOBS_URL = _BASE_URL + "/fr/companies/{slug}/jobs"

# Matches clean job-detail paths (no query params).
# Pattern: /fr/companies/<company>/jobs/<job-slug>
# Slug may contain letters, digits, hyphens, underscores, and dots.
_JOB_PATH_RE = re.compile(
    r'(?:href|url)[=:]\s*["\']?(/fr/companies/[A-Za-z0-9_-]+/jobs/[A-Za-z0-9_.-]+)["\']?',
)

# JSON-LD script block (non-greedy, handles multi-script pages).
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Employment type mapping: JSON-LD canonical value → normalizer value
_EMP_TYPE_MAP: dict[str, str] = {
    "FULL_TIME": "full-time",
    "PART_TIME": "part-time",
    "CONTRACTOR": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "VOLUNTEER": "contract",
    "PER_DIEM": "contract",
    "OTHER": "contract",
}


class WTTJCollector(BaseBrowserCollector):
    """Crawl4AI-based collector for Welcome to the Jungle company job pages.

    Configuration (sources.yaml → config sub-key):
        company_slugs        List of WTTJ company slugs to crawl.
        max_jobs_per_company Maximum job-detail pages to visit per company
                             (default: 20). Capped per the rate-limit budget.
        rate_limit_delay     Seconds between page requests (default: 3.0).
        timeout              Per-page timeout in seconds (default: 60).
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.company_slugs: list[str] = self.config.get("company_slugs") or []
        self.max_jobs_per_company: int = int(self.config.get("max_jobs_per_company", 20))

    # ------------------------------------------------------------------
    # Main collection entry
    # ------------------------------------------------------------------

    async def _async_collect(self) -> CollectorResult:
        self._require_crawl4ai()

        if not self.company_slugs:
            return CollectorResult(
                source_name="wttj",
                jobs=[],
                errors=["No company_slugs configured for the WTTJ collector"],
                duration_seconds=0.0,
            )

        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        start_time = time.time()
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []

        browser_cfg = BrowserConfig(headless=True, verbose=False)
        run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            for slug in self.company_slugs:
                listing_url = _COMPANY_JOBS_URL.format(slug=slug)
                try:
                    job_urls = await self._get_job_urls(crawler, run_cfg, listing_url)
                    logger.info("WTTJ: %d job URLs found for company '%s'", len(job_urls), slug)
                    for job_url in job_urls[: self.max_jobs_per_company]:
                        await self._async_sleep_rate_limit()
                        try:
                            raw = await self._extract_job(crawler, run_cfg, job_url)
                            if raw:
                                jobs.append(raw)
                            # raw=None means load succeeded but no JSON-LD found;
                            # _extract_job already logs a warning — not a hard error.
                        except _DetailPageError as exc:
                            errors.append(str(exc))
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"WTTJ: unexpected error extracting {job_url}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"WTTJ: failed to list jobs for '{slug}': {exc}")

        duration = time.time() - start_time
        self._log_collection("wttj", len(jobs), errors, duration)
        return CollectorResult(
            source_name="wttj",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Step 1: Discover job URLs from the company listing page
    # ------------------------------------------------------------------

    async def _get_job_urls(
        self,
        crawler: Any,
        run_cfg: Any,
        listing_url: str,
    ) -> list[str]:
        """Render the company jobs page and return all job-detail URLs."""
        await self._async_sleep_rate_limit()
        result = await crawler.arun(url=listing_url, config=run_cfg)
        if not result.success:
            raise RuntimeError(
                f"Page load failed ({result.status_code}): {result.error_message}"
            )
        return _extract_job_urls_from_html(result.html or "")

    # ------------------------------------------------------------------
    # Step 2: Extract structured data from a job detail page
    # ------------------------------------------------------------------

    async def _extract_job(
        self,
        crawler: Any,
        run_cfg: Any,
        job_url: str,
    ) -> dict[str, Any] | None:
        """Render a job detail page and extract the JSON-LD job posting.

        Raises _DetailPageError when the page fails to load — the caller
        records this in CollectorResult.errors.  Returns None (not an error)
        when the page loads but contains no JSON-LD.
        """
        result = await crawler.arun(url=job_url, config=run_cfg)
        if not result.success:
            raise _DetailPageError(
                f"WTTJ: failed to load {job_url} ({result.status_code}): {result.error_message}"
            )

        ld = _extract_job_posting_ld(result.html or "")
        if not ld:
            logger.warning("WTTJ: no schema.org/JobPosting JSON-LD found at %s", job_url)
            return None

        return _map_ld_to_raw(ld, job_url)


# ---------------------------------------------------------------------------
# Pure helpers (no Crawl4AI dependency — testable without a browser)
# ---------------------------------------------------------------------------

def _extract_job_urls_from_html(html: str) -> list[str]:
    """Return deduplicated absolute job-detail URLs found in the HTML."""
    seen: set[str] = set()
    urls: list[str] = []
    for path in _JOB_PATH_RE.findall(html):
        # Strip any trailing fragment or query string that snuck through
        clean = path.split("?")[0].split("#")[0]
        if clean not in seen:
            seen.add(clean)
            urls.append(_BASE_URL + clean)
    return urls


def _extract_job_posting_ld(html: str) -> dict[str, Any] | None:
    """Return the first schema.org/JobPosting JSON-LD object from HTML, or None."""
    for m in _JSON_LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
    return None


def _map_ld_to_raw(ld: dict[str, Any], canonical_url: str) -> dict[str, Any]:
    """Map schema.org/JobPosting fields to the pipeline normalizer contract."""
    org: dict[str, Any] = ld.get("hiringOrganization") or {}

    # Primary job location
    raw_locations = ld.get("jobLocation") or []
    addr: dict[str, Any] = {}
    if raw_locations:
        first = raw_locations[0] if isinstance(raw_locations[0], dict) else {}
        addr = first.get("address") or {}

    city: str = (addr.get("addressLocality") or "").strip()
    region: str = (addr.get("addressRegion") or "").strip()
    country_code: str = (addr.get("addressCountry") or "").strip()
    postal_code: str = (addr.get("postalCode") or "").strip()

    location_parts = [p for p in [city, region] if p]
    if country_code and country_code not in location_parts:
        location_parts.append(country_code)
    location = ", ".join(location_parts)

    # INSEE department code from French postal code
    department_code: str | None = None
    if postal_code and country_code == "FR":
        dept = postal_code[:3] if postal_code.startswith("97") else postal_code[:2]
        if dept and (dept.isdigit() or dept in ("2A", "2B")):
            department_code = dept

    # Employment type
    raw_emp = (ld.get("employmentType") or "").strip().upper()
    employment_type: str | None = _EMP_TYPE_MAP.get(raw_emp) or (raw_emp.lower() or None)

    # Work mode from description text (deterministic keyword match)
    description: str = ld.get("description") or ""
    work_mode = _detect_work_mode(description)

    # Source job ID: last path segment of the job URL (stable WTTJ slug)
    source_job_id: str | None = None
    if "/jobs/" in canonical_url:
        source_job_id = canonical_url.rstrip("/").rsplit("/jobs/", 1)[-1] or None

    return {
        "title": (ld.get("title") or "").strip(),
        "company": (org.get("name") or "").strip() or None,
        "description": description,
        "location": location,
        "countries": [country_code] if country_code else [],
        "department_code": department_code,
        "canonical_url": canonical_url,
        "source_job_id": source_job_id,
        "published_at": ld.get("datePosted") or None,
        "type": employment_type,
        "remote": work_mode == "remote",
        "work_mode": work_mode,
        "skills": [],
    }


def _detect_work_mode(description: str) -> str | None:
    """Return 'remote', 'hybrid', or None based on description text.

    Uses conservative keyword matching on the lowercased text.  Only returns
    'remote' for explicit full-remote signals; 'hybrid' for partial-remote
    signals.  Returns None when no signal is found rather than assuming on-site.
    """
    text = description.lower()
    full_remote_signals = (
        "100% remote", "fully remote", "full remote",
        "100% télétravail", "full-remote", "télétravail complet",
        "100% en télétravail",
    )
    hybrid_signals = (
        "télétravail", "telework", "remote possible", "hybrid", "hybride",
        "travail à distance", "remote autorisé",
    )
    if any(s in text for s in full_remote_signals):
        return "remote"
    if any(s in text for s in hybrid_signals):
        return "hybrid"
    return None
