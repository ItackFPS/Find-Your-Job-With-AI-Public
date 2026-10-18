"""Jobicy remote jobs collector.

Fetches remote jobs from https://jobicy.com/api/v2/remote-jobs — a free,
unauthenticated JSON API for remote job listings worldwide.

API docs: https://jobi.cy/apidocs (verified 2026-08-28)
Limits: no authentication required. The API returns up to 200 jobs per
request via the ?count= parameter. No pagination — one request per query.

Supported filter parameters (verified 2026-08-28):
  count     — max jobs to return (1–200, API hard cap)
  industry  — industrySlug from /api/v2/remote-jobs?get=industries
  geo       — geoSlug from /api/v2/remote-jobs?get=locations (e.g. "anywhere", "europe")
  tag       — free-form tech tag keyword (e.g. "python")

Multi-query mode:
  When discovery_queries is provided (a list of industry slugs injected by
  the orchestrator), the collector issues one request per industry slug.
  Results are deduplicated by job ID within the run.  A global max_jobs
  budget caps total output regardless of query count.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_URL = "https://jobicy.com/api/v2/remote-jobs"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; job-aggregator/1.0)",
}

# Default geo slug: "anywhere" maximises recall across all remote jobs.
# Other useful values (verified): "europe", "france", "emea".
_DEFAULT_GEO = "anywhere"

_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "full-time": "full-time",
    "full_time": "full-time",
    "part-time": "part-time",
    "part_time": "part-time",
    "contract": "contract",
    "freelance": "freelance",
    "internship": "internship",
}


class JobicyCollector(BaseCollector):
    """Collects remote jobs from the Jobicy public API.

    Configuration (sources.yaml → config sub-key):
        count            Jobs per request, max 200 (default: 200).
        geo              Geo slug to filter by (default: "anywhere").
        max_jobs         Total unique jobs to collect across all queries (default: unlimited).
        discovery_queries  List of Jobicy industry slugs injected by the orchestrator.
                           When provided, one request per slug is issued.
                           Falls back to a single unfiltered request when absent.
    """

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        super().__init__(timeout=30, retries=3, rate_limit_delay=2.0)
        cfg = config or {}
        self.max_jobs: int | None = cfg.get("max_jobs")
        self.count: int = min(int(cfg.get("count", 200)), 200)
        self.geo: str = str(cfg.get("geo") or _DEFAULT_GEO)
        # discovery_queries: list of industry slugs (injected by orchestrator)
        self.discovery_queries: list[str] = list(cfg.get("discovery_queries") or [])

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []
        seen_ids: set[int | str] = set()

        # Always run the unfiltered baseline first; industry slugs are subsets of the full
        # feed, not additive, so omitting the baseline would lose jobs not covered by any slug.
        queries: list[str | None] = [None]
        if self.discovery_queries:
            queries.extend(self.discovery_queries)

        for industry_slug in queries:
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break

            params: dict[str, Any] = {"count": self.count, "geo": self.geo}
            if industry_slug is not None:
                params["industry"] = industry_slug

            try:
                self._enforce_rate_limit()
                response = self._fetch_with_retry(_API_URL, headers=_HEADERS, params=params)
                data = response.json()
                raw_jobs: list[Any] = data.get("jobs") or []

                new_on_query = 0
                for raw in raw_jobs:
                    if not isinstance(raw, dict):
                        continue
                    job_id: int | str = raw.get("id") or raw.get("jobSlug") or ""
                    if job_id and job_id in seen_ids:
                        continue
                    if job_id:
                        seen_ids.add(job_id)
                    jobs.append(self._normalize_fields(raw))
                    new_on_query += 1
                    if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                        break

                logger.info(
                    "Jobicy: industry=%s geo=%s fetched=%d new=%d total=%d",
                    industry_slug or "(none)", self.geo, len(raw_jobs), new_on_query, len(jobs),
                )

            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
                errors.append(f"Jobicy industry={industry_slug}: {exc}")

        duration = time.time() - start_time
        self._log_collection("jobicy", len(jobs), errors, duration)
        return CollectorResult(
            source_name="jobicy",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    @staticmethod
    def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
        """Map Jobicy field names to the normalizer contract."""
        out = dict(raw)
        out["title"] = raw.get("jobTitle") or ""
        out["company"] = raw.get("companyName") or ""
        out["source_job_id"] = str(raw.get("id") or raw.get("jobSlug") or "")
        out["url"] = raw.get("url") or ""
        out["description"] = raw.get("jobDescription") or raw.get("jobExcerpt") or ""
        out["location"] = raw.get("jobGeo") or "Worldwide"
        out["remote"] = True  # Jobicy is a remote-only job board
        out["countries"] = []  # remote; scope determined by jobGeo field downstream
        out["published_at"] = raw.get("pubDate") or ""
        # Employment type
        job_types: list[Any] = raw.get("jobType") or []
        if isinstance(job_types, list) and job_types:
            raw_type = str(job_types[0]).lower().strip()
            out["employment_type"] = _EMPLOYMENT_TYPE_MAP.get(raw_type, raw_type)
        # Salary
        if raw.get("salaryMin"):
            try:
                out["salary_min"] = float(raw["salaryMin"])
            except (TypeError, ValueError):
                pass
        if raw.get("salaryMax"):
            try:
                out["salary_max"] = float(raw["salaryMax"])
            except (TypeError, ValueError):
                pass
        currency = raw.get("salaryCurrency") or "USD"
        out["salary_currency"] = currency
        # Skills from industry tags
        industries: list[Any] = raw.get("jobIndustry") or []
        if isinstance(industries, list):
            out["skills"] = [str(i) for i in industries if i]
        return out
