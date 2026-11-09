"""Employers' own career boards, through the public APIs their recruiting tools publish.

Replaces scraping job boards for offers employers also post themselves. Greenhouse, Lever,
SmartRecruiters, Ashby and Recruitee each document an unauthenticated read API meant for
third-party career pages, one board per employer, so a run costs one request per employer and
reads the whole board with its full description.

There is no directory of employers. ``boards`` comes from the offers already collected
(:mod:`src.discovery.ats_boards`): an offer whose link points at one of these tools names the
employer's board, so coverage grows with what the other sources find, for any line of work.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy
from src.collectors.greenhouse import GreenhouseCollector
from src.collectors.lever import LeverCollector, _parse_location_countries
from src.collectors.smartrecruiters import SmartRecruitersCollector

logger = logging.getLogger(__name__)

_MAX_JOBS_PER_BOARD = 500
_HEADERS = {"Accept": "application/json"}
_DELEGATED = {"greenhouse": GreenhouseCollector, "lever": LeverCollector, "smartrecruiters": SmartRecruitersCollector}


def _date(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def map_ashby(raw: dict[str, Any], slug: str) -> dict[str, Any]:
    """An Ashby ``posting-api`` job as the normalizer's raw dict."""
    location = str(raw.get("location") or "").strip()
    remote = bool(raw.get("isRemote"))
    return {
        "title": str(raw.get("title") or "").strip(),
        "company": slug.replace("-", " ").title(),
        "description": raw.get("descriptionPlain") or raw.get("descriptionHtml") or "",
        "location": location,
        "countries": _parse_location_countries(location),
        "remote": remote,
        "work_mode": "remote" if remote else None,
        "canonical_url": raw.get("jobUrl"),
        "apply_url": raw.get("applyUrl"),
        "source_job_id": str(raw.get("id") or "") or None,
        "published_at": _date(raw.get("publishedAt")),
        "type": raw.get("employmentType"),
        "skills": [],
    }


def map_recruitee(raw: dict[str, Any], slug: str) -> dict[str, Any]:
    """A Recruitee ``api/offers`` entry as the normalizer's raw dict."""
    location = str(raw.get("location") or raw.get("city") or "").strip()
    country = str(raw.get("country") or "").strip()
    remote = bool(raw.get("remote"))
    return {
        "title": str(raw.get("title") or "").strip(),
        "company": str(raw.get("company_name") or slug.replace("-", " ").title()),
        "description": " ".join(str(raw.get(k) or "") for k in ("description", "requirements")).strip(),
        "location": location,
        "countries": [country] if country else _parse_location_countries(location),
        "remote": remote,
        "work_mode": "remote" if remote else None,
        "canonical_url": raw.get("careers_url"),
        "apply_url": raw.get("careers_apply_url"),
        "source_job_id": str(raw.get("id") or "") or None,
        "published_at": _date(raw.get("published_at") or raw.get("created_at")),
        "type": raw.get("employment_type_code"),
        "skills": [],
    }


class AtsBoardsCollector(BaseCollector):
    """Config: ``boards``, a list of ``{"ats": name, "slug": employer}``."""

    access_class = "official"       # documented, unauthenticated read APIs
    rate_policy = RatePolicy(min_delay_seconds=0.5, max_requests_per_run=600)

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        super().__init__(rate_limit_delay=float(cfg.get("rate_limit_delay", 0.5)), timeout=int(cfg.get("timeout", 20)))
        self.boards = [b for b in cfg.get("boards") or [] if isinstance(b, dict) and b.get("ats") and b.get("slug")]

    def collect(self) -> CollectorResult:
        start = time.time()
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        for board in self.boards:
            ats, slug = str(board["ats"]), str(board["slug"])
            try:
                self._enforce_rate_limit()
                found, board_errors = self._read_board(ats, slug)
            except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{ats}/{slug}: {exc}")     # an employer leaving a tool is routine
                continue
            jobs.extend(j for j in found[:_MAX_JOBS_PER_BOARD] if j.get("title"))
            errors.extend(f"{ats}/{slug}: {e}" for e in board_errors)
        duration = time.time() - start
        self._log_collection("ats_boards", len(jobs), errors, duration)
        return CollectorResult(source_name="ats_boards", jobs=jobs, errors=errors, duration_seconds=duration,
                               metadata={"boards": len(self.boards)})

    def _read_board(self, ats: str, slug: str) -> tuple[list[dict[str, Any]], list[str]]:
        if ats in _DELEGATED:
            result = _DELEGATED[ats](config={"board_identifier": slug, "max_jobs": _MAX_JOBS_PER_BOARD}).collect()
            return result.jobs, result.errors
        if ats == "ashby":
            payload = self._fetch_with_retry(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=_HEADERS,
                                             params={"includeCompensation": "false"}).json()
            return [map_ashby(j, slug) for j in payload.get("jobs") or [] if j.get("isListed", True)], []
        if ats == "recruitee":
            payload = self._fetch_with_retry(f"https://{slug}.recruitee.com/api/offers/", headers=_HEADERS).json()
            return [map_recruitee(j, slug) for j in payload.get("offers") or []], []
        return [], [f"unknown tool {ats!r}"]
