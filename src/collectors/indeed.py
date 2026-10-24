"""Indeed job collector using the Indeed mobile GraphQL API.

Uses the same endpoint as python-jobspy (MIT licence) — apis.indeed.com/graphql —
which is the backend powering Indeed's iOS app.  No browser or Crawl4AI required.

API access:
  Endpoint : https://apis.indeed.com/graphql
  Auth     : the iOS app's API key, read from the INDEED_API_KEY environment variable
  Protocol : HTTPS POST, JSON body, GraphQL query string
  Locale   : fr-FR (France) controlled by 'indeed-co' / 'indeed-locale' headers

Phase 0 benchmark (2026-08-31, this machine):
  100 jobs/page, cursor pagination, 5 pages in 7.1 s, zero blocking, 100% France,
  100% descriptions included — no second detail-page pass needed.

API key:
  The endpoint needs the API key of Indeed's iOS app. It is Indeed's credential, not
  this project's, so it is not shipped here: set INDEED_API_KEY (e.g. in .env). The
  value python-jobspy (MIT licence) ships in jobspy/indeed/constant.py works. Without
  it the collector makes no request and reports one error. Indeed can revoke the key
  at any time; update INDEED_API_KEY if that happens.

Query strategy:
  The collector runs one paginated search per configured query (keyword + location).
  It also accepts 'discovery_queries' from the orchestrator (keyword-only strings,
  run against default_location) for profile-driven query generation.

Robots.txt:
  The /graphql path on apis.indeed.com is a mobile backend, not a crawlable page.
  No robots.txt restrictions apply to JSON API clients.

Indeed Terms of Service:
  Automated access is technically prohibited.  This collector is for personal,
  local, research use only.  Use conservatively.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API configuration — ALL Indeed-specific constants live here.
# To update the endpoint or key, change only this section.
# ---------------------------------------------------------------------------

_API_URL = "https://apis.indeed.com/graphql"
_BASE_URL = "https://fr.indeed.com"
_JOB_URL_TEMPLATE = _BASE_URL + "/viewjob?jk={key}"

# Headers of Indeed's iOS app, without its API key (see _api_key()).
# Source reference: python-jobspy (MIT licence), jobspy/indeed/constant.py.
_API_HEADERS: dict[str, str] = {
    "Host": "apis.indeed.com",
    "content-type": "application/json",
    "accept": "application/json",
    "indeed-locale": "fr-FR",
    "accept-language": "fr-FR,fr;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Indeed App 193.1"
    ),
    "indeed-app-info": (
        "appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone"
    ),
    "indeed-co": "FR",
}

API_KEY_ENV = "INDEED_API_KEY"


def _api_key() -> str:
    """The Indeed iOS app API key from the environment, or "" when unset."""
    return os.environ.get(API_KEY_ENV, "").strip()

_JOBS_PER_PAGE = 100  # API maximum; hardcoded here so tests can reference it

# GraphQL query template.  Double-braces escape GraphQL literal braces from
# Python str.format().  Placeholders: {keyword}, {location}, {limit},
# {cursor_line} (empty or 'cursor: "..."'), {date_filter} (empty or filters block).
_GQL_QUERY_TMPL = """\
query GetJobData {{
    jobSearch(
    what: "{keyword}"
    location: {{where: "{location}", radius: 50, radiusUnit: MILES}}
    limit: {limit}
    {cursor_line}
    sort: RELEVANCE
    {date_filter}
    ) {{
    pageInfo {{
        nextCursor
    }}
    results {{
        job {{
        key
        title
        datePublished
        description {{
            html
        }}
        location {{
            countryName
            countryCode
            city
            postalCode
            admin1Code
            formatted {{
            short
            long
            }}
        }}
        compensation {{
            estimated {{
            currencyCode
            baseSalary {{
                unitOfWork
                range {{ ... on Range {{ min max }} }}
            }}
            }}
            baseSalary {{
            unitOfWork
            range {{ ... on Range {{ min max }} }}
            }}
            currencyCode
        }}
        attributes {{
            key
            label
        }}
        employer {{
            relativeCompanyPageUrl
            name
        }}
        recruit {{
            viewJobUrl
            workSchedule
        }}
        }}
    }}
    }}
}}"""

# Work-mode attribute keys (fr-FR locale, from 1218-job census).
_REMOTE_ATTR_KEY = "DSQF7"    # 'Télétravail' — full remote
_HYBRID_ATTR_KEYS = frozenset(("PAXZC", "N83EH"))  # 'Télétravail partiel' / 'Travail à domicile occasionnel'
_ONSITE_ATTR_KEY = "SWG7T"    # 'En présentiel' — explicitly on-site

# Location strings that confirm a remote/hybrid position.
_REMOTE_LOC_SIGNALS = ("télétravail", "teletravail", "remote", "travail à distance")
_HYBRID_LOC_SIGNALS = ("hybride", "hybrid", "télétravail partiel", "teletravail partiel")

# Employment type: lowercased attribute label → canonical pipeline value.
_EMP_TYPE_MAP: dict[str, str] = {
    "temps plein": "full-time",
    "full-time": "full-time",
    "temps partiel": "part-time",
    "part-time": "part-time",
    "stage": "internship",
    "internship": "internship",
    "alternance": "alternance",
    "contrat": "contract",
    "contract": "contract",
    "freelance": "contract",
    "intérim": "contract",
    "interim": "contract",
    "cdi": "full-time",
    "cdd": "contract",
}


# ---------------------------------------------------------------------------
# IndeedCollector
# ---------------------------------------------------------------------------

class IndeedCollector(BaseCollector):
    """Collects job listings from Indeed via the mobile GraphQL API.

    Configuration (sources.yaml → config sub-key):
        queries             List of {keyword, location} dicts.
        discovery_queries   List of keyword strings injected by the orchestrator;
                            each is run against default_location.
        default_location    Location for discovery_queries (default: "France").
        max_pages_per_query Maximum pages per query, 100 jobs each (default: 5).
        max_jobs_per_query  Hard cap on jobs collected per query (default: unlimited).
        date_filter_hours   Only return jobs posted in the last N hours (default: 720 = 30 days).
        rate_limit_delay    Seconds between page requests (default: 0.5).
        timeout             Per-request timeout in seconds (default: 20).
    """

    # unofficial: Indeed's mobile-app GraphQL endpoint, called with the app's key. Indeed
    # has no public API (the Publisher API ended), so this is opt-in once consent exists
    # (plan F6.5). No robots.txt check: it is an app endpoint, not a crawled page.
    access_class = "unofficial"
    # The shipped config runs ~25 queries x 5 pages; its 0.5 s delay is the floor.
    rate_policy = RatePolicy(min_delay_seconds=0.5, max_requests_per_run=500)

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("indeed_graphql.json", self._load_fixture("indeed_graphql.json"),
                               parse_graphql_response)

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 20)),
            retries=int(cfg.get("retries", 3)),
            rate_limit_delay=float(cfg.get("rate_limit_delay", 0.5)),
        )
        self.queries: list[dict[str, str]] = list(cfg.get("queries") or [])
        self.discovery_queries: list[str] = list(cfg.get("discovery_queries") or [])
        self.default_location: str = str(cfg.get("default_location", "France"))
        self.max_pages_per_query: int = int(cfg.get("max_pages_per_query", 5))
        self.max_jobs_per_query: int | None = (
            int(cfg["max_jobs_per_query"]) if cfg.get("max_jobs_per_query") else None
        )
        self.date_filter_hours: int | None = (
            int(cfg["date_filter_hours"]) if cfg.get("date_filter_hours") else None
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def collect(self) -> CollectorResult:
        start_time = time.time()
        all_jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        seen_keys: set[str] = set()

        # Build the full query list: configured queries + discovery_queries.
        effective_queries: list[tuple[str, str]] = []
        for q in self.queries:
            kw = str(q.get("keyword") or "").strip()
            loc = str(q.get("location") or self.default_location).strip()
            if kw:
                effective_queries.append((kw, loc))
        for kw in self.discovery_queries:
            kw = str(kw).strip()
            if kw:
                effective_queries.append((kw, self.default_location))

        api_key = _api_key()
        if not api_key:
            logger.warning("IndeedCollector: %s is not set — skipping Indeed", API_KEY_ENV)
            return CollectorResult(
                source_name="indeed",
                jobs=[],
                errors=[f"indeed: {API_KEY_ENV} is not set; see .env.example"],
                duration_seconds=0.0,
            )

        if not effective_queries:
            logger.warning("IndeedCollector: no queries configured — nothing to collect")
            return CollectorResult(
                source_name="indeed",
                jobs=[],
                errors=["No queries configured"],
                duration_seconds=0.0,
            )

        for keyword, location in effective_queries:
            query_jobs, query_errors = self._collect_query(keyword, location, seen_keys)
            all_jobs.extend(query_jobs)
            errors.extend(query_errors)

        duration = time.time() - start_time
        self._log_collection("indeed", len(all_jobs), errors, duration)
        return CollectorResult(
            source_name="indeed",
            jobs=all_jobs,
            errors=errors,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Per-query collection
    # ------------------------------------------------------------------

    def _collect_query(
        self,
        keyword: str,
        location: str,
        seen_keys: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Paginate through Indeed results for one keyword+location pair.

        Stops when: cursor exhausted, no results, all keys duplicated,
        max_pages or max_jobs reached, or an API error occurs.
        Updates seen_keys in-place for cross-query deduplication.
        """
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        cursor: str | None = None
        page = 0

        while page < self.max_pages_per_query:
            if self.max_jobs_per_query and len(jobs) >= self.max_jobs_per_query:
                logger.info(
                    "indeed: keyword='%s' max_jobs_per_query (%d) reached",
                    keyword, self.max_jobs_per_query,
                )
                break

            self._enforce_rate_limit()
            try:
                page_jobs, next_cursor, error = self._fetch_page(keyword, location, cursor)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"indeed: keyword='{keyword}' page {page + 1}: unexpected error: {exc}")
                break

            if error:
                errors.append(f"indeed: keyword='{keyword}' page {page + 1}: {error}")
                break

            if not page_jobs:
                logger.info(
                    "indeed: keyword='%s' page %d: no results — stopping pagination",
                    keyword, page + 1,
                )
                break

            new_count = 0
            all_duplicates = True
            for raw in page_jobs:
                key = raw.get("source_job_id") or ""
                if key and key in seen_keys:
                    continue
                all_duplicates = False
                if key:
                    seen_keys.add(key)
                jobs.append(raw)
                new_count += 1

            logger.info(
                "indeed: keyword='%s' page %d: fetched=%d new=%d total=%d",
                keyword, page + 1, len(page_jobs), new_count, len(jobs),
            )

            if all_duplicates:
                logger.info(
                    "indeed: keyword='%s' page %d: all keys already seen — stopping",
                    keyword, page + 1,
                )
                break

            if not next_cursor:
                logger.info(
                    "indeed: keyword='%s' page %d: no next cursor — end of results",
                    keyword, page + 1,
                )
                break

            cursor = next_cursor
            page += 1

        return jobs, errors

    # ------------------------------------------------------------------
    # Single page fetch
    # ------------------------------------------------------------------

    def _fetch_page(
        self,
        keyword: str,
        location: str,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """POST one page to the Indeed GraphQL API.

        Returns (raw_jobs, next_cursor, error_string).
        error_string is None on success; raw_jobs is [] and next_cursor is None
        on error.
        """
        query = _build_graphql_query(
            keyword=keyword,
            location=location,
            cursor=cursor,
            date_filter_hours=self.date_filter_hours,
        )
        last_error: str | None = None
        self._before_request(_API_URL)  # the per-run request cap

        for attempt in range(1, self.retries + 1):
            try:
                logger.debug(
                    "indeed: POST page keyword='%s' cursor=%s (attempt %d/%d)",
                    keyword, bool(cursor), attempt, self.retries,
                )
                resp = httpx.post(
                    _API_URL,
                    headers={**_API_HEADERS, "indeed-api-key": _api_key()},
                    json={"query": query},
                    timeout=self.timeout,
                )
                self._observe_response(resp)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"network error: {exc}"
                if attempt < self.retries:
                    backoff = 2 ** (attempt - 1) * 0.5
                    logger.warning(
                        "indeed: attempt %d network error: %s; retrying in %.1fs",
                        attempt, exc, backoff,
                    )
                    time.sleep(backoff)
                continue

            # Permanent failures (auth, bad request) — stop immediately.
            if resp.status_code in (401, 403):
                return [], None, (
                    f"API key rejected (HTTP {resp.status_code}). "
                    f"Update {API_KEY_ENV}."
                )
            if resp.status_code == 400:
                return [], None, f"Bad GraphQL request (HTTP 400): {resp.text[:200]}"

            # Transient failures — retry.
            if resp.status_code in (408, 429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}"
                if attempt < self.retries:
                    wait = self._retry_delay(resp, attempt)
                    logger.warning(
                        "indeed: attempt %d got HTTP %d; retrying in %.1fs",
                        attempt, resp.status_code, wait,
                    )
                    time.sleep(wait)
                continue

            if not resp.is_success:
                return [], None, f"HTTP {resp.status_code}: {resp.text[:200]}"

            # Parse JSON response.
            try:
                data = resp.json()
            except Exception as exc:
                last_error = f"JSON parse error: {exc}"
                if attempt < self.retries:
                    time.sleep(0.5)
                continue

            if "errors" in data:
                gql_errors = data["errors"]
                return [], None, f"GraphQL errors: {gql_errors[:2]}"

            job_search = (data.get("data") or {}).get("jobSearch") or {}
            results = job_search.get("results") or []
            next_cursor: str | None = (
                (job_search.get("pageInfo") or {}).get("nextCursor") or None
            )

            raw_jobs = [
                _map_job_to_raw(r["job"])
                for r in results
                if isinstance(r, dict) and isinstance(r.get("job"), dict)
            ]
            return raw_jobs, next_cursor, None

        return [], None, f"indeed: all {self.retries} attempts failed: {last_error}"


# ---------------------------------------------------------------------------
# Pure helpers — completely testable without any network access
# ---------------------------------------------------------------------------

def _build_graphql_query(
    keyword: str,
    location: str,
    cursor: str | None = None,
    date_filter_hours: int | None = None,
) -> str:
    """Build the GraphQL query string for one page request."""
    cursor_line = f'cursor: "{cursor}"' if cursor else ""
    date_filter = ""
    if date_filter_hours:
        date_filter = (
            f'filters: {{ date: {{ field: "dateOnIndeed", start: "{date_filter_hours}h" }} }}'
        )
    return _GQL_QUERY_TMPL.format(
        keyword=keyword.replace('"', '\\"'),
        location=location.replace('"', '\\"'),
        limit=_JOBS_PER_PAGE,
        cursor_line=cursor_line,
        date_filter=date_filter,
    )


def _dept_from_postal(postal_code: str, country_code: str) -> str | None:
    """Derive the French INSEE department code from a 5-digit postal code.

    Handles metropolitan France, Corsica, and overseas departments.
    Returns None for non-French countries or malformed postal codes.
    """
    if country_code != "FR" or not postal_code:
        return None
    code = str(postal_code).strip()
    if len(code) != 5 or not code.isdigit():
        return None
    if code.startswith("97"):
        dept = code[:3]
        return dept if dept in ("971", "972", "973", "974", "976") else None
    prefix = code[:2]
    if prefix == "20":
        try:
            return "2A" if int(code[2:]) <= 199 else "2B"
        except ValueError:
            return None
    # Validate: department codes 01–95 exist; 96–99 do not.
    try:
        num = int(prefix)
        return prefix if 1 <= num <= 95 else None
    except ValueError:
        return None


def _parse_salary(
    compensation: dict[str, Any] | None,
) -> tuple[float | None, float | None, str]:
    """Extract salary min/max and currency from the compensation dict.

    Prefers the employer-stated baseSalary over Indeed's estimated value.
    Returns (None, None, 'EUR') when no salary data is present.
    Note: amounts are in the unit stated by 'unitOfWork' (YEAR/MONTH/HOUR);
    no unit normalisation is performed here — the raw values are passed through.
    """
    if not compensation:
        return None, None, "EUR"

    currency = (
        (compensation.get("estimated") or {}).get("currencyCode")
        or compensation.get("currencyCode")
        or "EUR"
    )

    base = compensation.get("baseSalary")
    if not base:
        est = compensation.get("estimated") or {}
        base = est.get("baseSalary")

    if not base:
        return None, None, currency

    rng = base.get("range") or {}
    sal_min = rng.get("min")
    sal_max = rng.get("max")
    try:
        sal_min = float(sal_min) if sal_min is not None else None
        sal_max = float(sal_max) if sal_max is not None else None
    except (TypeError, ValueError):
        sal_min = sal_max = None

    return sal_min, sal_max, currency


def _parse_employment_type(attributes: list[dict[str, Any]]) -> str | None:
    """Map Indeed attribute labels to a canonical employment type string."""
    for attr in attributes:
        label = str(attr.get("label") or "").strip().lower()
        mapped = _EMP_TYPE_MAP.get(label)
        if mapped:
            return mapped
    return None


def _parse_work_mode(
    attributes: list[dict[str, Any]],
    location_long: str,
) -> tuple[str | None, bool]:
    """Determine work_mode ('remote'/'hybrid'/'on_site'/None) and the remote bool.

    Attribute keys take precedence over location strings. Priority:
      1. DSQF7 ('Télétravail') → remote
      2. PAXZC ('Télétravail partiel') or N83EH ('Travail à domicile occasionnel') → hybrid
      3. SWG7T ('En présentiel') → on_site
      4. Location string contains remote signals → remote
      5. Location string contains hybrid signals → hybrid
      6. None (unknown) — caller must not assume on_site

    Returns (work_mode, remote_bool).
    """
    attr_keys = {str(a.get("key") or "") for a in attributes}
    if _REMOTE_ATTR_KEY in attr_keys:
        return "remote", True
    if attr_keys & _HYBRID_ATTR_KEYS:
        return "hybrid", False
    if _ONSITE_ATTR_KEY in attr_keys:
        return "on_site", False

    loc_lower = location_long.lower()
    if any(sig in loc_lower for sig in _REMOTE_LOC_SIGNALS):
        return "remote", True
    if any(sig in loc_lower for sig in _HYBRID_LOC_SIGNALS):
        return "hybrid", False

    return None, False


def _parse_published_at(date_published_ms: int | float | None) -> str | None:
    """Convert an Indeed epoch-millisecond timestamp to an ISO-8601 string.

    Returns None when the value is missing, zero, or non-numeric.
    """
    if not date_published_ms:
        return None
    try:
        ts = float(date_published_ms) / 1000.0
        if ts <= 0:
            return None
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError, OSError):
        return None


def parse_graphql_response(text: str) -> list[dict[str, Any]]:
    """The offers in one jobSearch GraphQL answer, mapped like _fetch_page() maps them."""
    data = json.loads(text)
    job_search = ((data.get("data") or {}).get("jobSearch") or {}) if isinstance(data, dict) else {}
    return [_map_job_to_raw(r["job"]) for r in job_search.get("results") or []
            if isinstance(r, dict) and isinstance(r.get("job"), dict)]


def _map_job_to_raw(job: dict[str, Any]) -> dict[str, Any]:
    """Map a single Indeed GraphQL job result to the pipeline normalizer contract.

    This is a pure function: no I/O, no side effects, no external dependencies.
    All field extraction and type conversion happens here.
    """
    key: str = str(job.get("key") or "").strip()

    # Location
    loc: dict[str, Any] = job.get("location") or {}
    city: str = str(loc.get("city") or "").strip()
    postal: str = str(loc.get("postalCode") or "").strip()
    country_code: str = str(loc.get("countryCode") or "").strip().upper()
    loc_long: str = str((loc.get("formatted") or {}).get("long") or city).strip()

    dept_code = _dept_from_postal(postal, country_code)
    countries = [country_code] if country_code else []

    # Attributes
    attributes: list[dict[str, Any]] = job.get("attributes") or []

    # Work mode
    work_mode, is_remote = _parse_work_mode(attributes, loc_long)

    # Employment type
    employment_type = _parse_employment_type(attributes)

    # Salary
    salary_min, salary_max, currency = _parse_salary(job.get("compensation"))

    # Publication date
    published_at = _parse_published_at(job.get("datePublished"))

    # Description (HTML — stripped by the normalizer later)
    description: str = str(
        (job.get("description") or {}).get("html") or ""
    ).strip()

    # Company and URLs
    employer: dict[str, Any] = job.get("employer") or {}
    company_name: str | None = employer.get("name") or None
    company_rel_url: str | None = employer.get("relativeCompanyPageUrl") or None
    company_url: str | None = (
        (_BASE_URL + company_rel_url) if company_rel_url else None
    )

    recruit: dict[str, Any] = job.get("recruit") or {}
    apply_url: str | None = recruit.get("viewJobUrl") or None

    canonical_url: str | None = _JOB_URL_TEMPLATE.format(key=key) if key else None

    return {
        "source_job_id": key or None,
        "title": str(job.get("title") or "").strip(),
        "company": company_name,
        "description": description,
        "location": loc_long,
        "countries": countries,
        "department_code": dept_code,
        "work_mode": work_mode,
        "remote": is_remote,
        "type": employment_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency,
        "published_at": published_at,
        "canonical_url": canonical_url,
        "apply_url": apply_url,
        "company_url": company_url,
        "skills": [],
    }
