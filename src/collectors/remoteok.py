"""RemoteOK public job board collector.

Fetches remote jobs from https://remoteok.com/api — a free, unauthenticated
JSON API for remote job listings worldwide.

API notes (verified 2026-08-28):
  - The first element of the returned array is a metadata object, not a job.
  - Rate limiting: be conservative (2 s between requests).
  - No pagination; each endpoint returns the most recent ~100 jobs for the tag.
  - Tag filter: GET /api?tags=<tag> returns jobs tagged with that term.
    Verified working for: python, javascript, devops, react, and others.

Multi-query mode:
  When discovery_queries is provided (a list of tech tags injected by the
  orchestrator), the collector issues one request per tag.  Results are
  deduplicated by slug/id within the run.  Falls back to a single unfiltered
  /api request when absent.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_URL = "https://remoteok.com/api"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; job-aggregator/1.0)",
}


class RemoteOKCollector(BaseCollector):
    """Collects remote jobs from the RemoteOK public API.

    Configuration (sources.yaml → config sub-key):
        max_jobs         Total unique jobs to collect across all queries (default: unlimited).
        discovery_queries  List of English tech tags (injected by orchestrator).
                           One request per tag. Falls back to unfiltered when absent.
    """

    # official: RemoteOK's public JSON API (remoteok.com/api), offered for reuse with a
    # link back; robots.txt (checked 2026-10-24) allows it.
    access_class = "official"

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        super().__init__(timeout=30, retries=3, rate_limit_delay=2.0)
        cfg = config or {}
        self.max_jobs: int | None = cfg.get("max_jobs")
        # discovery_queries: list of tech tags injected by the orchestrator.
        self.discovery_queries: list[str] = list(cfg.get("discovery_queries") or [])

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # One request per tag; fall back to unfiltered when no queries.
        tags: list[str | None] = list(self.discovery_queries) if self.discovery_queries else [None]

        for tag in tags:
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break

            params: dict[str, Any] = {}
            if tag is not None:
                params["tags"] = tag

            try:
                self._enforce_rate_limit()
                response = self._fetch_with_retry(_API_URL, headers=_HEADERS, params=params)
                entries: list[Any] = response.json()

                if not isinstance(entries, list):
                    errors.append(f"RemoteOK tag={tag}: unexpected response type")
                    continue

                new_on_query = 0
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    # Skip the metadata header object (has no 'slug' or 'position').
                    if "slug" not in entry and "position" not in entry:
                        continue
                    job_id = str(entry.get("id") or entry.get("slug") or "")
                    if job_id and job_id in seen_ids:
                        continue
                    if job_id:
                        seen_ids.add(job_id)
                    jobs.append(self._normalize_fields(entry))
                    new_on_query += 1
                    if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                        break

                logger.info(
                    "RemoteOK: tag=%s fetched=%d new=%d total=%d",
                    tag or "(none)", len(entries) - 1, new_on_query, len(jobs),
                )

            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
                errors.append(f"RemoteOK tag={tag}: {exc}")

        duration = time.time() - start_time
        self._log_collection("remoteok", len(jobs), errors, duration)
        return CollectorResult(
            source_name="remoteok",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    @staticmethod
    def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
        """Map RemoteOK field names to the normalizer contract."""
        out = dict(raw)
        out["title"] = raw.get("position") or raw.get("title") or ""
        out["company"] = raw.get("company") or ""
        out["source_job_id"] = str(raw.get("id") or raw.get("slug") or "")
        out["url"] = raw.get("url") or raw.get("apply_url") or ""
        out["apply_url"] = raw.get("apply_url") or ""
        out["description"] = raw.get("description") or ""
        out["location"] = raw.get("location") or "Worldwide"
        out["remote"] = True  # RemoteOK is a remote-only job board
        out["countries"] = []  # remote-worldwide; let filter decide scope
        out["published_at"] = raw.get("date") or ""
        # Salary
        if raw.get("salary_min"):
            out["salary_min"] = _parse_number(raw["salary_min"])
        if raw.get("salary_max"):
            out["salary_max"] = _parse_number(raw["salary_max"])
        out["salary_currency"] = "USD"
        # Skills from tags
        tags = raw.get("tags") or []
        if isinstance(tags, list):
            out["skills"] = [t for t in tags if isinstance(t, str)]
        return out


def _parse_number(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
