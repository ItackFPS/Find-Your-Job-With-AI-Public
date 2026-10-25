"""Greenhouse job board collector.

Fetches job postings from one or more companies' public Greenhouse boards
via the official Job Board API:
  GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

The API is unauthenticated and explicitly designed for embedding public career
pages.  Each Greenhouse customer's board_token is their URL slug (e.g.
"doctolib" for Doctolib, "algolia" for Algolia).

A 404 for an unknown board_token is handled gracefully and skipped — the same
pattern used for SmartRecruiters.  A misconfigured board_token returns no jobs
rather than aborting the pipeline.

Verified companies and board_tokens (2026-08-28):
  doctolib → doctolib  (12 jobs)
  algolia  → algolia   (10 jobs)
  datadog  → datadog   (117 jobs, 5 in Paris)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_HEADERS = {"Accept": "application/json"}


class GreenhouseCollector(BaseCollector):
    """Collects jobs from one or more Greenhouse job boards.

    Configuration (sources.yaml → config sub-key):
        board_identifiers  List of board token strings (company slugs).
        board_identifier   Single token; merged with board_identifiers if both given.
        rate_limit_delay   Seconds between requests (default: 2.0).
        timeout            Per-request timeout in seconds (default: 30).
        max_jobs           Optional hard cap across all boards.
    """

    # official: Greenhouse's documented public Job Board API (boards-api.greenhouse.io),
    # published so employers' boards can be embedded and reused.
    access_class = "official"

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 3)),
            rate_limit_delay=float(cfg.get("rate_limit_delay", 2.0)),
        )
        # Collect board_identifiers from both possible config keys.
        identifiers: list[str] = list(cfg.get("board_identifiers") or [])
        single = cfg.get("board_identifier") or cfg.get("board_token")
        if single and single not in identifiers:
            identifiers.append(single)
        self.board_identifiers = [str(b).strip() for b in identifiers if str(b).strip()]
        self.max_jobs: int | None = cfg.get("max_jobs")

    def collect(self) -> CollectorResult:
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        if not self.board_identifiers:
            errors.append(
                "Greenhouse collector requires at least one board token in "
                "'board_identifiers' (or 'board_identifier'). "
                "None were configured; skipping collection."
            )
            duration = time.time() - start_time
            self._log_collection("greenhouse", 0, errors, duration)
            return CollectorResult(source_name="greenhouse", jobs=jobs, errors=errors,
                                   duration_seconds=duration)

        for token in self.board_identifiers:
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break
            self._enforce_rate_limit()
            self._collect_board(token, jobs, errors)

        duration = time.time() - start_time
        self._log_collection("greenhouse", len(jobs), errors, duration)
        return CollectorResult(source_name="greenhouse", jobs=jobs, errors=errors,
                               duration_seconds=duration)

    def _collect_board(
        self, token: str, jobs: list[dict[str, Any]], errors: list[str]
    ) -> None:
        """Fetch all published jobs for one Greenhouse board token."""
        url = f"{_API_BASE}/{token}/jobs"
        try:
            response = self._fetch_with_retry(url, headers=_HEADERS,
                                               params={"content": "true"})
            data = response.json()
            raw_jobs: list[Any] = data.get("jobs") or []
            before = len(jobs)
            for raw in raw_jobs:
                if not isinstance(raw, dict):
                    continue
                mapped = _map_job(raw, token)
                if mapped.get("title"):
                    jobs.append(mapped)
                if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                    break
            logger.info(
                "Greenhouse: board=%s fetched=%d",
                token, len(jobs) - before,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                errors.append(
                    f"Greenhouse: board '{token}' not found (HTTP 404); "
                    "verify the board_token."
                )
            else:
                errors.append(f"Greenhouse: board '{token}' HTTP error: {exc}")
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"Greenhouse: board '{token}' failed: {exc}")


# ---------------------------------------------------------------------------
# Pure helpers — testable without network
# ---------------------------------------------------------------------------

def _map_job(raw: dict[str, Any], board_token: str) -> dict[str, Any]:
    """Map a Greenhouse job dict to the pipeline normalizer raw-dict contract."""
    location_obj = raw.get("location") or {}
    location_name = str(location_obj.get("name") or "").strip()
    countries = _parse_location_countries(location_name)

    source_job_id = str(raw.get("id") or "").strip() or None
    absolute_url = raw.get("absolute_url") or None
    company_name = (raw.get("company_name") or board_token.replace("-", " ").title()).strip()

    # Employment type from metadata array (key "Employment Type" if present).
    employment_type: str | None = None
    for meta in raw.get("metadata") or []:
        if isinstance(meta, dict) and str(meta.get("name") or "").lower() == "employment type":
            employment_type = str(meta.get("value") or "").strip() or None
            break

    published_at_raw = raw.get("first_published") or raw.get("updated_at") or None

    return {
        "title": (raw.get("title") or "").strip(),
        "company": company_name,
        "description": raw.get("content") or "",   # HTML; stripped by normalizer
        "location": location_name,
        "countries": countries,
        "remote": "remote" in location_name.lower(),
        "work_mode": _parse_work_mode(location_name),
        "canonical_url": absolute_url,
        "source_job_id": source_job_id,
        "published_at": published_at_raw,
        "type": employment_type,
        "skills": [],
    }


def _parse_location_countries(location: str) -> list[str]:
    """Infer countries from a location string like 'Paris, France'."""
    loc = location.lower()
    _MAP = [
        (["france", "paris", "lyon", "marseille", "bordeaux", "nantes",
          "toulouse", "lille", "rennes", "grenoble", "strasbourg", "montpellier"], "France"),
        (["germany", "berlin", "munich", "hamburg", "frankfurt", "cologne",
          "düsseldorf", "stuttgart", "dusseldorf"], "Germany"),
        (["united kingdom", "england", "scotland", "wales", "london",
          "manchester", "edinburgh", " uk,", "uk "], "United Kingdom"),
        (["spain", "barcelona", "madrid", "valencia", "seville", "bilbao"], "Spain"),
        (["netherlands", "amsterdam", "rotterdam", "utrecht", "eindhoven"], "Netherlands"),
        (["belgium", "brussels", "antwerp", "ghent"], "Belgium"),
        (["switzerland", "zurich", "geneva", "bern", "basel"], "Switzerland"),
        (["ireland", "dublin", "cork"], "Ireland"),
        (["portugal", "lisbon", "porto"], "Portugal"),
        (["italy", "rome", "milan", "turin", "florence", "naples"], "Italy"),
        (["poland", "warsaw", "krakow", "wroclaw", "gdansk", "poznan"], "Poland"),
        (["sweden", "stockholm", "gothenburg", "malmo"], "Sweden"),
        (["denmark", "copenhagen"], "Denmark"),
        (["finland", "helsinki"], "Finland"),
        (["norway", "oslo"], "Norway"),
        (["austria", "vienna", "graz"], "Austria"),
    ]
    for keywords, country in _MAP:
        if any(kw in loc for kw in keywords):
            return [country]
    return []


def _parse_work_mode(location: str) -> str | None:
    """Derive work_mode from a Greenhouse location string."""
    loc = location.lower()
    if "remote" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    return None
