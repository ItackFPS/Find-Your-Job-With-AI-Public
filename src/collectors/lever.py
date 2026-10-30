"""Lever job board collector.

Fetches job postings from one or more companies' public Lever boards via the
official unauthenticated Postings API (v0):
  GET https://api.lever.co/v0/postings/{site}?mode=json

The API is documented at https://github.com/lever/postings-api and is designed
for building custom career pages.  A 404 for an unknown site is handled
gracefully (the company may have left Lever or use a different ATS).

Verified companies and site slugs (2026-08-28):
  contentsquare → contentsquare  (3+ jobs incl. Paris)
  ledger        → ledger          (1 job)
  theodo        → theodo          (2 jobs, Paris, AI-focused CDI)
  qonto         → qonto           (2 jobs, Paris)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_BASE = "https://api.lever.co/v0/postings"
_HEADERS = {"Accept": "application/json"}

_WORK_MODE_MAP: dict[str, str] = {
    "remote": "remote",
    "hybrid": "hybrid",
    "on-site": "on_site",
    "onsite": "on_site",
}


class LeverCollector(BaseCollector):
    """Collects jobs from one or more Lever job boards.

    Configuration (sources.yaml → config sub-key):
        sites              List of Lever site slug strings (company slugs).
        board_identifier   Single site slug; merged with sites if both given.
        rate_limit_delay   Seconds between requests (default: 2.0).
        timeout            Per-request timeout in seconds (default: 30).
        max_jobs           Optional hard cap across all sites.
    """

    # official: Lever's documented public Postings API (github.com/lever/postings-api),
    # designed for employers' job boards to be read by third parties.
    access_class = "official"

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 3)),
            rate_limit_delay=float(cfg.get("rate_limit_delay", 2.0)),
        )
        sites: list[str] = list(cfg.get("sites") or [])
        single = cfg.get("board_identifier") or cfg.get("site")
        if single and single not in sites:
            sites.append(single)
        self.sites = [str(s).strip() for s in sites if str(s).strip()]
        self.max_jobs: int | None = cfg.get("max_jobs")

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        if not self.sites:
            errors.append(
                "Lever collector requires at least one site slug in "
                "'sites' (or 'board_identifier'). "
                "None were configured; skipping collection."
            )
            duration = time.time() - start_time
            self._log_collection("lever", 0, errors, duration)
            return CollectorResult(source_name="lever", jobs=jobs, errors=errors,
                                   duration_seconds=duration)

        for site in self.sites:
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break
            self._enforce_rate_limit()
            self._collect_site(site, jobs, errors)

        duration = time.time() - start_time
        self._log_collection("lever", len(jobs), errors, duration)
        return CollectorResult(source_name="lever", jobs=jobs, errors=errors,
                               duration_seconds=duration,
                               listing_complete=not errors and self.max_jobs is None)

    def _collect_site(
        self, site: str, jobs: list[dict[str, Any]], errors: list[str]
    ) -> None:
        """Fetch all open postings for one Lever site slug."""
        url = f"{_API_BASE}/{site}"
        try:
            response = self._fetch_with_retry(url, headers=_HEADERS,
                                               params={"mode": "json"})
            raw_list: list[Any] = response.json()
            if not isinstance(raw_list, list):
                errors.append(
                    f"Lever: site '{site}' returned unexpected format: {type(raw_list)}"
                )
                return
            before = len(jobs)
            company_name = site.replace("-", " ").title()
            for raw in raw_list:
                if not isinstance(raw, dict):
                    continue
                mapped = _map_job(raw, site, company_name)
                if mapped.get("title"):
                    jobs.append(mapped)
                if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                    break
            logger.info("Lever: site=%s fetched=%d", site, len(jobs) - before)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                errors.append(
                    f"Lever: site '{site}' not found (HTTP 404); "
                    "verify the site slug."
                )
            else:
                errors.append(f"Lever: site '{site}' HTTP error: {exc}")
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"Lever: site '{site}' failed: {exc}")


# ---------------------------------------------------------------------------
# Pure helpers — testable without network
# ---------------------------------------------------------------------------

def _map_job(raw: dict[str, Any], site: str, company_name: str) -> dict[str, Any]:
    """Map a Lever posting dict to the pipeline normalizer raw-dict contract."""
    categories = raw.get("categories") or {}
    location = str(categories.get("location") or "").strip()
    country_raw = str(raw.get("country") or "").strip()
    commitment = str(categories.get("commitment") or "").strip()

    countries = [country_raw] if country_raw else _parse_location_countries(location)

    workplace_type = str(raw.get("workplaceType") or "").lower()
    work_mode = _WORK_MODE_MAP.get(workplace_type)

    source_job_id = str(raw.get("id") or "").strip() or None
    hosted_url = raw.get("hostedUrl") or None
    apply_url = raw.get("applyUrl") or None

    # createdAt is a Unix timestamp in milliseconds.
    created_at_ms = raw.get("createdAt")
    published_at: datetime | None = None
    if created_at_ms:
        try:
            published_at = datetime.fromtimestamp(
                int(created_at_ms) / 1000.0, tz=timezone.utc
            )
        except (TypeError, ValueError, OSError):
            pass

    # Prefer plain-text description; fall back to HTML (normalizer strips it).
    description = (raw.get("descriptionPlain") or raw.get("description") or "").strip()
    # Append any additional text sections (Lever "lists").
    for lst in raw.get("lists") or []:
        if isinstance(lst, dict):
            content = lst.get("content") or ""
            if content:
                description += "\n" + content

    return {
        "title": (raw.get("text") or "").strip(),
        "company": company_name,
        "description": description,
        "location": location,
        "countries": countries,
        "remote": work_mode == "remote",
        "work_mode": work_mode,
        "canonical_url": hosted_url,
        "apply_url": apply_url,
        "source_job_id": source_job_id,
        "published_at": published_at,
        "type": commitment or None,
        "skills": [],
    }


def _parse_location_countries(location: str) -> list[str]:
    """Infer countries from a Lever location string like 'Paris, France'."""
    loc = location.lower()
    _MAP = [
        (["france", "paris", "lyon", "marseille", "bordeaux", "nantes",
          "toulouse", "lille", "rennes", "grenoble", "strasbourg"], "France"),
        (["germany", "berlin", "munich", "hamburg", "frankfurt", "cologne",
          "düsseldorf", "dusseldorf", "stuttgart"], "Germany"),
        (["united kingdom", "england", "london", "manchester",
          "edinburgh", " uk,", "uk "], "United Kingdom"),
        (["spain", "barcelona", "madrid", "valencia"], "Spain"),
        (["netherlands", "amsterdam", "rotterdam", "utrecht"], "Netherlands"),
        (["belgium", "brussels", "antwerp", "ghent"], "Belgium"),
        (["switzerland", "zurich", "geneva", "bern", "basel"], "Switzerland"),
        (["ireland", "dublin", "cork"], "Ireland"),
        (["portugal", "lisbon", "porto"], "Portugal"),
        (["italy", "rome", "milan", "turin", "naples"], "Italy"),
        (["poland", "warsaw", "krakow", "wroclaw", "gdansk"], "Poland"),
        (["sweden", "stockholm", "gothenburg"], "Sweden"),
        (["austria", "vienna", "graz"], "Austria"),
    ]
    for keywords, country in _MAP:
        if any(kw in loc for kw in keywords):
            return [country]
    return []
