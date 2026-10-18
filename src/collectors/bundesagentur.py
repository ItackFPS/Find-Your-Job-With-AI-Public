"""Bundesagentur für Arbeit (German Federal Employment Agency) collector.

Fetches jobs from the "Jobsuche" REST API that backs the BA's own official
jobsuche app/website: https://rest.arbeitsagentur.de/jobboerse/jobsuche-service

This API has no official developer documentation from the BA itself, but it
is the BA's real production backend (not a scrape of rendered HTML), and its
client id / access key is publicly documented and used by the German open
government-data community (bundesAPI/jobsuche-api on GitHub, mirrored at
jobsuche.api.bund.dev). No registration is required.

Authentication:
  Header 'X-API-Key: jobboerse-jobsuche' (the BA's own public web client id).

Verified 2026-08-27:
  - v4 endpoint returns HTTP 403 ("No match found request") for some queries.
  - v6 endpoint (pc/v6/jobs) works reliably and returns real data.
  - No SSL/certificate issues when called via httpx — the previously reported
    SSL problem was not reproducible against this endpoint.

Quota / rate limit: undocumented (unofficial API). Collector is conservative:
one request per configured search term, small page size, rate-limited.

Limitation: the search endpoint does not include a job description; fetching
full descriptions requires one extra request per job
(pc/v4/jobdetails/{base64(referenznummer)}), which would multiply request
count by the number of results. To keep collection fast, this collector does
not fetch per-job descriptions; jobs are normalized from title/metadata only.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
_HEADERS = {
    "Accept": "application/json",
    "X-API-Key": "jobboerse-jobsuche",
}
_DETAIL_URL_TEMPLATE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"

# Broad set of tech-adjacent German search terms. Kept small to bound the
# number of requests per run (one request per term).
_DEFAULT_SEARCH_TERMS = [
    "Softwareentwickler",
    "IT",
    "DevOps",
    "Data Engineer",
]


class BundesagenturCollector(BaseCollector):
    """Collects German job listings from the BA Jobsuche API."""

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        super().__init__(timeout=30, retries=3, rate_limit_delay=1.0)
        cfg = config or {}
        self.max_jobs: int | None = cfg.get("max_jobs")
        self.search_terms: list[str] = cfg.get("search_terms") or _DEFAULT_SEARCH_TERMS
        self.size: int = min(int(cfg.get("size", 50)), 100)

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []
        seen_refs: set[str] = set()

        for term in self.search_terms:
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break
            try:
                self._enforce_rate_limit()
                response = self._fetch_with_retry(
                    _API_URL,
                    headers=_HEADERS,
                    params={"was": term, "size": self.size, "angebotsart": 1},
                )
                data = response.json()
                raw_jobs: list[Any] = data.get("ergebnisliste") or []

                for raw in raw_jobs:
                    if not isinstance(raw, dict):
                        continue
                    ref = raw.get("referenznummer") or ""
                    if ref and ref in seen_refs:
                        continue
                    if ref:
                        seen_refs.add(ref)
                    jobs.append(self._normalize_fields(raw))
                    if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                        break

            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
                errors.append(f"Bundesagentur search term='{term}' failed: {exc}")

        duration = time.time() - start_time
        self._log_collection("bundesagentur", len(jobs), errors, duration)
        return CollectorResult(
            source_name="bundesagentur",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    @staticmethod
    def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
        """Map BA Jobsuche field names to the normalizer contract."""
        out: dict[str, Any] = {}
        out["title"] = raw.get("stellenangebotsTitel") or ""
        out["company"] = raw.get("firma") or ""
        out["description"] = ""  # not available without a per-job detail request

        location = ""
        locations = raw.get("stellenlokationen") or []
        if isinstance(locations, list) and locations:
            addr = (locations[0] or {}).get("adresse") or {}
            ort = addr.get("ort") or ""
            region = addr.get("region") or ""
            location = ", ".join(p for p in (ort, region) if p)
        out["location"] = location or "Germany"
        out["countries"] = ["Germany"]
        out["remote"] = bool(raw.get("homeofficemoeglich"))
        # "homeofficemoeglich" means "home office is possible", not that the
        # role is fully remote — it's a weaker signal than `remote=True`
        # implies. Recorded more precisely as "hybrid" in work_mode without
        # touching the legacy `remote` boolean above (kept for backward
        # compatibility). False/absent stays unknown (None): the flag's
        # absence doesn't confirm the role is on-site.
        out["work_mode"] = "hybrid" if raw.get("homeofficemoeglich") else None

        if raw.get("arbeitszeitVollzeit"):
            out["employment_type"] = "full-time"
        elif any(
            raw.get(k)
            for k in (
                "arbeitszeitTeilzeitAbend",
                "arbeitszeitTeilzeitNachmittag",
                "arbeitszeitTeilzeitVormittag",
                "arbeitszeitTeilzeitFlexibel",
            )
        ):
            out["employment_type"] = "part-time"

        ref = raw.get("referenznummer") or ""
        out["source_job_id"] = ref
        external_url = raw.get("externeURL")
        if external_url:
            out["url"] = external_url
        elif ref:
            encoded = base64.urlsafe_b64encode(ref.encode()).decode()
            out["url"] = _DETAIL_URL_TEMPLATE.format(ref=encoded)

        out["published_at"] = raw.get("datumErsteVeroeffentlichung") or ""
        return out
