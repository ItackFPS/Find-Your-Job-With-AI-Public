"""Working Nomads remote jobs collector.

Fetches remote jobs from https://www.workingnomads.com/api/exposed_jobs/ — a
free, unauthenticated JSON API for curated remote job listings.

API notes:
  - No authentication required.
  - The documented ?category= filter is a no-op: verified 2026-08-27 that
    requests with category=development, design, devops-sysadmin,
    management-finance, product, and data-analytics, and no category at all,
    all return the exact same 41 jobs (identical URL set). The endpoint does
    not actually support server-side category filtering, so this collector
    makes a single unfiltered request instead of one request per category
    (previously ~6 sequential requests, ~30s+ for zero additional coverage).
  - No pagination; the endpoint returns all currently listed jobs (~40–100).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_URL = "https://www.workingnomads.com/api/exposed_jobs/"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; job-aggregator/1.0)",
}


class WorkingNomadsCollector(BaseCollector):
    """Collects remote jobs from the Working Nomads public API."""

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        super().__init__(timeout=30, retries=3, rate_limit_delay=2.0)
        cfg = config or {}
        self.max_jobs: int | None = cfg.get("max_jobs")

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        try:
            self._enforce_rate_limit()
            response = self._fetch_with_retry(_API_URL, headers=_HEADERS)
            raw_list: list[Any] = response.json()

            if not isinstance(raw_list, list):
                errors.append(f"Unexpected response type: {type(raw_list)}")
                raw_list = []

            for raw in raw_list:
                if not isinstance(raw, dict):
                    continue
                jobs.append(self._normalize_fields(raw))
                if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                    break

        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"Collection failed: {exc}")

        duration = time.time() - start_time
        self._log_collection("workingnomads", len(jobs), errors, duration)
        return CollectorResult(
            source_name="workingnomads",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    @staticmethod
    def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
        """Map Working Nomads field names to the normalizer contract."""
        out = dict(raw)
        out["title"] = raw.get("title") or ""
        out["company"] = raw.get("company_name") or ""
        out["url"] = raw.get("url") or ""
        out["source_job_id"] = raw.get("url") or ""  # URL is the stable identifier
        out["description"] = raw.get("description") or ""
        out["location"] = raw.get("location") or "Worldwide"
        out["remote"] = True
        out["countries"] = []
        out["published_at"] = raw.get("pub_date") or ""
        # Tags as skills
        tags = raw.get("tags") or ""
        if isinstance(tags, str) and tags:
            out["skills"] = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            out["skills"] = [str(t) for t in tags if t]
        return out
