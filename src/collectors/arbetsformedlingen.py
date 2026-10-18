"""Arbetsförmedlingen (Swedish Public Employment Service) collector.

Fetches jobs from the official, free, public JobSearch API operated by
JobTech Dev — the open-data/open-API developer platform of Arbetsförmedlingen:
https://jobsearch.api.jobtechdev.se

This is a genuinely official government API (distinct from Arbetsförmedlingen's
restricted, authenticated "Partner APIs"). No API key, registration, or
authentication is required. Documented at https://arbetsformedlingen.se and
https://gitlab.com/arbetsformedlingen/job-ads/jobsearch/jobsearch-api.

Verified 2026-08-27: GET /search?q=developer&limit=3 -> HTTP 200, real data.

Quota / rate limit: not publicly documented as a numeric limit; treated as
"reasonable free use" per JobTech Dev's stated open-data policy. Collector
uses a conservative rate-limit delay and bounded page size.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_URL = "https://jobsearch.api.jobtechdev.se/search"
_HEADERS = {"Accept": "application/json"}

# Broad set of tech-adjacent search terms (English terms match well since
# many Swedish tech job ads are written in English). One request per term.
_DEFAULT_SEARCH_TERMS = [
    "developer",
    "software engineer",
    "devops",
    "data engineer",
]


class ArbetsformedlingenCollector(BaseCollector):
    """Collects Swedish job listings from the official JobTech JobSearch API."""

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        super().__init__(timeout=30, retries=3, rate_limit_delay=1.0)
        cfg = config or {}
        self.max_jobs: int | None = cfg.get("max_jobs")
        self.search_terms: list[str] = cfg.get("search_terms") or _DEFAULT_SEARCH_TERMS
        self.limit: int = min(int(cfg.get("limit", 50)), 100)

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for term in self.search_terms:
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break
            try:
                self._enforce_rate_limit()
                response = self._fetch_with_retry(
                    _API_URL,
                    headers=_HEADERS,
                    params={"q": term, "limit": self.limit},
                )
                data = response.json()
                raw_jobs: list[Any] = data.get("hits") or []

                for raw in raw_jobs:
                    if not isinstance(raw, dict):
                        continue
                    job_id = str(raw.get("id") or "")
                    if job_id and job_id in seen_ids:
                        continue
                    if job_id:
                        seen_ids.add(job_id)
                    jobs.append(self._normalize_fields(raw))
                    if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                        break

            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
                errors.append(f"Arbetsformedlingen search term='{term}' failed: {exc}")

        duration = time.time() - start_time
        self._log_collection("arbetsformedlingen", len(jobs), errors, duration)
        return CollectorResult(
            source_name="arbetsformedlingen",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    @staticmethod
    def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
        """Map JobTech JobSearch field names to the normalizer contract."""
        out: dict[str, Any] = {}
        out["title"] = raw.get("headline") or ""

        employer = raw.get("employer") or {}
        out["company"] = employer.get("name") or ""

        description = raw.get("description") or {}
        out["description"] = description.get("text") or ""

        address = raw.get("workplace_address") or {}
        city = address.get("municipality") or ""
        region = address.get("region") or ""
        out["location"] = ", ".join(p for p in (city, region) if p) or "Sweden"
        out["countries"] = ["Sweden"]

        workplace_model = (raw.get("workplace_model") or {}).get("label") or ""
        model_lower = workplace_model.lower()
        out["remote"] = "distans" in model_lower or "hybrid" in model_lower

        hours_label = ((raw.get("working_hours_type") or {}).get("label") or "").lower()
        if "heltid" in hours_label:
            out["employment_type"] = "full-time"
        elif "deltid" in hours_label:
            out["employment_type"] = "part-time"

        out["url"] = raw.get("webpage_url") or ""
        out["source_job_id"] = str(raw.get("id") or "")
        out["published_at"] = raw.get("publication_date") or ""
        return out
