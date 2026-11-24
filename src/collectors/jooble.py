"""Jooble collector through Jooble's official REST API (#312).

API (help.jooble.org "REST API Documentation", modified 2026-11-11):
  POST https://fr.jooble.org/api/{key}   JSON body: keywords, location, radius, page, ResultOnPage
  -> {"totalCount": N, "jobs": [{id, title, location, snippet, salary, source, type, link, company,
     updated}]}
  One key per country domain (a jooble.org key reads US offers only), 100 requests per day per key,
  reset at midnight UTC. The key is sent by e-mail after a registration on fr.jooble.org/api/about.

Why the API and nothing else (measured 2026-11-24): the site's search and job pages answer a
Cloudflare challenge to plain HTTP clients (403, ``cf-mitigated: challenge``), robots.txt disallows
``/SearchResult``, ``/desc/`` and ``/away/``, and the site terms (§4 m) forbid bots. Every open
Jooble scraper inspected either calls this API with a key or parses HTML that is challenged today;
none is used here, and no challenge is ever worked around.

What it gives: leads — title, company, place, salary text, a ~200-character snippet and a Jooble
link. No full description exists in the API; the same offer usually arrives in full from France
Travail, Indeed or an employer board, and the cross-source deduplication keeps one copy.

Configuration (sources.yaml -> config sub-key):
    api_key / JOOBLE_API_KEY  the France key (environment variable or config); none -> skipped with
                              an error, nothing requested.
    discovery_queries         keywords injected by the orchestrator; ``queries`` adds
                              {keyword, location} pairs.
    default_location          location for discovery queries (default "France").
    results_per_page          ResultOnPage (default 50).
    max_pages_per_query       pages per search (default 2).
    max_requests              requests per run (default 20: five runs a day fit the 100/day quota).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from html import unescape
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture

logger = logging.getLogger(__name__)

_API_URL = "https://fr.jooble.org/api/{key}"
_TAG_RE = re.compile(r"<[^>]+>")
_TYPE_MAP = {"cdi": "full-time", "temps plein": "full-time", "full-time": "full-time",
             "temps partiel": "part-time", "part-time": "part-time", "cdd": "contract",
             "intérim": "contract", "interim": "contract", "stage": "internship",
             "alternance": "internship", "apprentissage": "internship"}


class JoobleQuotaOrKeyError(RuntimeError):
    """Jooble refused the key (403) or the daily quota is spent (429): stop the run's searches."""


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", str(value or "")))).strip()


def _published_at(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).isoformat()


def map_job(raw: dict[str, Any]) -> dict[str, Any]:
    """One Jooble API job -> the pipeline normalizer contract (pure)."""
    kind = _text(raw.get("type")).lower()
    job_id = raw.get("id")
    return {
        "source_job_id": (str(job_id).strip() or None) if job_id is not None else None,
        "title": _text(raw.get("title")),
        "company": _text(raw.get("company")) or None,
        # The API's only text is a search snippet; the dedup prefers a fuller copy from elsewhere.
        "description": _text(raw.get("snippet")),
        "location": _text(raw.get("location")),
        "countries": ["FR"],
        "canonical_url": str(raw.get("link") or "").strip() or None,
        "published_at": _published_at(raw.get("updated")),
        "type": next((v for k, v in _TYPE_MAP.items() if k in kind), None),
        "remote": False,
        "work_mode": None,
        "skills": [],
        "salary_text": _text(raw.get("salary")) or None,
        "origin_source": _text(raw.get("source")) or None,
    }


def parse_search_response(text: str) -> list[dict[str, Any]]:
    """The offers in one API answer, mapped like collect() maps them."""
    data = json.loads(text)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    rows = [map_job(j) for j in (jobs if isinstance(jobs, list) else []) if isinstance(j, dict)]
    return [r for r in rows if r["source_job_id"] and r["title"]]


class JoobleCollector(BaseCollector):
    """Job leads from Jooble's official REST API (France key)."""

    # official: Jooble's documented partner REST API, with the user's own key.
    access_class = "official"
    # The free quota is 100 requests a day per key; 1 s between requests.
    rate_policy = RatePolicy(min_delay_seconds=1.0, max_requests_per_run=100)

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        cfg = config or {}
        super().__init__(timeout=int(cfg.get("timeout", 20)), retries=1,
                         rate_limit_delay=float(cfg.get("rate_limit_delay", 1.0)))
        self.api_key: str = str(cfg.get("api_key") or os.environ.get("JOOBLE_API_KEY", "")).strip()
        self.default_location: str = str(cfg.get("default_location") or "France")
        self.queries: list[dict[str, str]] = [
            {"keyword": str(q.get("keyword") or "").strip(),
             "location": str(q.get("location") or self.default_location).strip()}
            for q in (cfg.get("queries") or []) if str(q.get("keyword") or "").strip()]
        self.queries += [{"keyword": str(k).strip(), "location": self.default_location}
                         for k in (cfg.get("discovery_queries") or []) if str(k).strip()]
        self.results_per_page: int = max(1, int(cfg.get("results_per_page", 50)))
        self.max_pages_per_query: int = max(1, int(cfg.get("max_pages_per_query", 2)))
        self.max_requests: int = max(1, min(int(cfg.get("max_requests", 20)), 100))

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("jooble_search.json", self._load_fixture("jooble_search.json"),
                               parse_search_response)

    def collect(self) -> CollectorResult:
        start = time.time()
        if not self.api_key:
            return CollectorResult(source_name="jooble", jobs=[], duration_seconds=0.0, errors=[
                "Jooble: no API key (JOOBLE_API_KEY). Request a France key at fr.jooble.org/api/about; "
                "nothing was requested."])
        if not self.queries:
            return CollectorResult(source_name="jooble", jobs=[], duration_seconds=0.0,
                                   errors=["No queries configured"])
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        requests_made = 0
        per_query: dict[str, dict[str, Any]] = {}
        try:
            for q in self.queries:
                label = f"{q['keyword']} @ {q['location']}"
                stats = per_query.setdefault(label, {"pages": 0, "fetched": 0, "total": None, "stop": "page_cap"})
                for page in range(1, self.max_pages_per_query + 1):
                    if requests_made >= self.max_requests:
                        stats["stop"] = "request_budget"
                        break
                    requests_made += 1
                    try:
                        rows, total = self._fetch_page(q["keyword"], q["location"], page)
                    except JoobleQuotaOrKeyError:
                        raise
                    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                        errors.append(f"Jooble [{label}] page {page}: {exc}")
                        stats["stop"] = "error"
                        break
                    stats.update(pages=page, total=total)
                    for raw in rows:
                        key = raw["source_job_id"]
                        if key not in seen:
                            seen.add(key)
                            jobs.append(raw)
                    stats["fetched"] += len(rows)
                    if len(rows) < self.results_per_page:
                        stats["stop"] = "exhausted"
                        break
        except JoobleQuotaOrKeyError as exc:
            errors.append(f"Jooble: {exc}")
        duration = time.time() - start
        self._log_collection("jooble", len(jobs), errors, duration)
        return CollectorResult(source_name="jooble", jobs=jobs, errors=errors, duration_seconds=duration,
                               metadata={"per_query_status": per_query, "requests": requests_made})

    def _fetch_page(self, keyword: str, location: str, page: int) -> tuple[list[dict[str, Any]], int | None]:
        self._before_request(_API_URL.format(key="KEY"), check_robots=False)
        self._enforce_rate_limit()
        body = {"keywords": keyword, "location": location, "page": page, "ResultOnPage": self.results_per_page}
        response = httpx.post(_API_URL.format(key=self.api_key), json=body, timeout=self.timeout,
                              headers={"Content-Type": "application/json", "Accept": "application/json"})
        self._observe_response(response)
        if response.status_code == 403:
            raise JoobleQuotaOrKeyError("the API refused the key (HTTP 403): check JOOBLE_API_KEY (a France key)")
        if response.status_code == 429:
            raise JoobleQuotaOrKeyError("the daily quota (100 requests per key) is spent (HTTP 429)")
        response.raise_for_status()
        rows = parse_search_response(response.text)
        total = response.json().get("totalCount")
        if not rows:
            self._note_empty_result()
        return rows, int(total) if isinstance(total, int) else None
