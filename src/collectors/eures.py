"""EURES (European Employment Services) job collector.

Uses the public REST API that backs the EURES portal SPA.  No authentication
required — the same public endpoint used by the EURES website.

API: POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search

robots.txt (eures.europa.eu, verified 2026-08-27):
  No restrictions on /api/ paths.

Response shape:
  {"numberRecords": N, "jvs": [
      {"id": ..., "title": ..., "description": ...,
       "employer": {"name": ...}, "creationDate": unix_ms,
       "locationMap": {"FR": "FRI22"},
       "positionScheduleCodes": ["fulltime"],
       "positionOfferingCode": "directhire"}
  ]}

Pagination: 1-indexed ``page`` field in POST body.

Location: ``locationMap`` maps ISO-2 country codes to NUTS region codes.
The summary endpoint does not include a city string; country names derived
from the ISO-2 keys are used as the location string.

Work mode: not available in the summary response; left as None.

Canonical URL: constructed as
``https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=en``
using the ``id`` field present in every summary response object.

Query shape (#270, measured 2026-11-24 on France, page 1 of 50): ``MOST_RECENT`` with the
keyword matched ``EVERYWHERE`` returned 13-40 offers out of 50 whose title named the keyword
(infirmier 17, comptable 16, électricien 40, chauffeur poids lourd 13); ``BEST_MATCH`` returned
50/50 for every keyword, with the keyword in the title (``TITLE``) or anywhere. The default is
``BEST_MATCH`` + ``TITLE``: every page stays on topic, so paging deeper adds relevant offers.

France Travail bridge: a French EURES offer's id is base64 of ``"<France Travail id> 9"`` (9 =
France Travail's connection point; 100/100 decoded and 16/16 found on the France Travail API with
the same title, 2026-11-24). ``ft_offer_id`` carries that id, so the cross-source deduplication
recognises the same offer from both sources (src/pipeline/deduplicate.py).

France Travail fallback: when the run skips the France Travail API (its login failed, #303), the
orchestrator sets ``ft_unavailable`` and EURES, which carries France Travail's own offers, reads up
to ``fallback_max_pages_per_query`` pages per search instead of ``max_pages_per_query``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture

logger = logging.getLogger(__name__)

_API_URL = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# EURES uses ISO-2 country codes as locationMap keys.
_ISO2_TO_COUNTRY: dict[str, str] = {
    "FR": "France",
    "DE": "Germany",
    "BE": "Belgium",
    "NL": "Netherlands",
    "SE": "Sweden",
    "AT": "Austria",
    "CH": "Switzerland",
    "ES": "Spain",
    "IT": "Italy",
    "PL": "Poland",
    "PT": "Portugal",
    "IE": "Ireland",
    "CZ": "Czech Republic",
    "DK": "Denmark",
    "NO": "Norway",
    "FI": "Finland",
    "LU": "Luxembourg",
    "HU": "Hungary",
    "RO": "Romania",
    "GR": "Greece",
    "EL": "Greece",   # EURES uses EL for Greece (EU convention)
    "HR": "Croatia",
    "BG": "Bulgaria",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "EE": "Estonia",
    "LV": "Latvia",
    "LT": "Lithuania",
    "MT": "Malta",
    "CY": "Cyprus",
    "IS": "Iceland",
    "LI": "Liechtenstein",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
}

# France Travail's connection point on EURES (connection-point facet, 2026-11-23: 99.99 % of FR).
_FT_CONNECTION_POINT = "9"
_EURES_ID_RE = re.compile(r"(\S+) (\d+)")
_SORTS = frozenset({"BEST_MATCH", "MOST_RECENT"})
_KEYWORD_SCOPES = frozenset({"TITLE", "EVERYWHERE"})


def france_travail_id(eures_id: str | None) -> str | None:
    """The France Travail offer id behind a EURES id, or None (not base64 / another connection point)."""
    text = str(eures_id or "").strip()
    if not text:
        return None
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    m = _EURES_ID_RE.fullmatch(decoded)
    return m.group(1) if m and m.group(2) == _FT_CONNECTION_POINT else None


class EURESCollector(BaseCollector):
    """httpx-based collector for the EURES European job search API.

    Runs a configurable list of keyword + location-code searches and collects
    results up to ``max_pages_per_query`` pages per search.  Duplicate job IDs
    within a single run are skipped.

    Configuration (sources.yaml → config sub-key):
        max_pages_per_query  Pages to fetch per keyword query (default: 3).
        results_per_page     Jobs per page, capped at 100 (default: 100).
        rate_limit_delay     Seconds between any two requests (default: 2.0).
        timeout              Per-request timeout in seconds (default: 30).
        queries              List of {keyword, location_codes} dicts.
        discovery_queries    Keyword strings injected by the orchestrator from the
                             profile; each runs against default_location_codes.
        default_location_codes  Country codes for discovery_queries (default: ["fr"]).
        sort_search          BEST_MATCH (default) or MOST_RECENT.
        keyword_scope        TITLE (default) or EVERYWHERE: where the keyword must match.
        publication_period   Optional EURES period (LAST_DAY, LAST_THREE_DAYS, LAST_WEEK).
        ft_unavailable       Set by the orchestrator when the France Travail API is skipped.
        fallback_max_pages_per_query  Pages per search then (default: 10).
                             With neither queries nor discovery_queries nothing is
                             searched: no profession-specific default exists.
        concurrent_queries   How many independent queries to run in parallel
                             (default: 2). Each query's own pages are still
                             fetched strictly sequentially — only different
                             queries (which share no pagination state) may
                             overlap. Measured safe/beneficial up to 2 on the
                             live API (see src/collectors/eures.py module
                             docstring's performance note); higher values
                             showed negligible further gain in testing
                             because the shared rate-limit dispatch gate
                             (below) — not request concurrency — is the
                             binding constraint beyond that point.

    Concurrency and rate limiting
    ------------------------------
    Different queries (distinct keyword/location_codes pairs) share no
    pagination state and are safe to run concurrently — see the module's
    2026-09 performance investigation. What they DO share is the inherited
    rate limiter (``BaseCollector._enforce_rate_limit``), which is NOT
    thread-safe on its own; ``_rate_lock`` below serializes access to it so
    concurrent workers still respect the exact same "no less than
    ``rate_limit_delay`` seconds between successive request dispatches"
    cadence as the fully sequential implementation. Concurrency therefore
    never increases how fast new requests are ISSUED to the EURES API — it
    only lets an already-dispatched, still-in-flight request's server-side
    wait time overlap with the next dispatch instead of strictly stacking,
    which is where EURES's cost actually lives (~91% server response wait
    vs. ~9% rate-limit padding, measured).
    """

    # public_page: the EURES portal's own anonymous search endpoint, not documented as an
    # API for third parties, but europa.eu content is reusable (Commission Decision
    # 2011/833/EU) and robots.txt (europa.eu, checked 2026-10-24) does not disallow
    # /eures/api/. Read as a public page: robots.txt is checked before every request.
    access_class = "public_page"
    robots_policy = True
    # >= 1 s between requests; the shipped config makes ~10 queries x 5 pages = 50.
    rate_policy = RatePolicy(min_delay_seconds=1.0, max_requests_per_run=300)

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 3)),
            rate_limit_delay=float(cfg.get("rate_limit_delay", 2.0)),
        )
        self.max_pages_per_query: int = int(cfg.get("max_pages_per_query", 3))
        if cfg.get("ft_unavailable"):
            self.max_pages_per_query = max(self.max_pages_per_query,
                                           int(cfg.get("fallback_max_pages_per_query", 10)))
        sort = str(cfg.get("sort_search") or "BEST_MATCH").upper()
        self.sort_search: str = sort if sort in _SORTS else "BEST_MATCH"
        scope = str(cfg.get("keyword_scope") or "TITLE").upper()
        self.keyword_scope: str = scope if scope in _KEYWORD_SCOPES else "TITLE"
        period = cfg.get("publication_period")
        self.publication_period: str | None = str(period).upper() if period else None
        self.results_per_page: int = min(int(cfg.get("results_per_page", 50)), 50)
        self.queries: list[dict[str, Any]] = list(cfg.get("queries") or [])
        self.default_location_codes: list[str] = [
            str(c) for c in cfg.get("default_location_codes") or ["fr"]
        ]
        for keyword in cfg.get("discovery_queries") or []:
            if str(keyword).strip():
                self.queries.append(
                    {"keyword": str(keyword).strip(), "location_codes": self.default_location_codes}
                )
        self.concurrent_queries: int = max(1, int(cfg.get("concurrent_queries", 2)))
        self._rate_lock = threading.Lock()

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("eures_search.json", self._load_fixture("eures_search.json"),
                               parse_search_response)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def collect(self) -> CollectorResult:
        start_time = time.time()

        if not self.queries:
            logger.warning("EURESCollector: no queries configured — nothing to collect")
            return CollectorResult(source_name="eures", jobs=[], errors=["No queries configured"],
                                   duration_seconds=0.0)

        # Queries are independent (no shared pagination state — see class
        # docstring), so bounded concurrency runs them in parallel. Each
        # query's OWN page loop (_run_query) is completely unchanged from
        # the previous fully-sequential implementation: same rate-limit call
        # (now via the thread-safe _rate_lock wrapper), same _fetch_page,
        # same early-stop-on-short-page condition, same retry/error
        # handling. Only the ACROSS-query scheduling changed.
        with ThreadPoolExecutor(max_workers=self.concurrent_queries) as pool:
            futures = [pool.submit(self._run_query, query) for query in self.queries]
            # .result() in submission order (== self.queries order), not
            # completion order — this is what makes the merge below
            # deterministic and identical to the old single-threaded run.
            per_query_results = [f.result() for f in futures]
        # complete only if every query read its whole result list (Phase B B4-D): a short page, or
        # the page cap reached with every offer the API counted already fetched
        listing_complete = all(done for _, _, done, _ in per_query_results)
        per_query_status = {
            f"{q.get('keyword')} [{','.join(q.get('location_codes') or [])}]": status
            for q, (_, _, _, status) in zip(self.queries, per_query_results)
        }

        # Merge in original query-list order, first-occurrence-wins — the
        # exact same semantics as the previous single shared `seen_ids` set
        # threaded through one sequential loop, just applied as one
        # deterministic pass after all (independently, safely concurrent)
        # per-query fetches have completed, instead of interleaved with
        # them. This avoids any race on a set mutated from multiple threads
        # while producing an identical final result.
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        seen_ids: set[str] = set()
        total_raw = 0
        for raw_jobs_for_query, errors_for_query, _, _ in per_query_results:
            errors.extend(errors_for_query)
            total_raw += len(raw_jobs_for_query)
            for raw in raw_jobs_for_query:
                job_id = str(raw.get("id") or "").strip()
                if job_id and job_id in seen_ids:
                    continue
                if job_id:
                    seen_ids.add(job_id)
                mapped = _map_job(raw)
                if mapped.get("title"):
                    jobs.append(mapped)

        logger.info(
            "EURES: %d queries, %d raw fetched, %d unique after cross-query dedup",
            len(self.queries), total_raw, len(jobs),
        )

        duration = time.time() - start_time
        self._log_collection("eures", len(jobs), errors, duration)
        return CollectorResult(
            source_name="eures",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
            metadata={"per_query_status": per_query_status},
            listing_complete=listing_complete and not errors,
        )

    def _run_query(
        self, query: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str], bool, dict[str, Any]]:
        """Fetch all pages for one query — unchanged sequential logic.

        Identical to the per-query body of the previous single-threaded
        ``collect()`` loop, minus the (now deferred, see ``collect()``)
        cross-query deduplication. Safe to run concurrently with other
        queries: no shared mutable state besides the rate limiter, which is
        already made thread-safe via ``self._rate_lock``.

        Returns (raw jv dicts collected across this query's pages, error
        strings for this query, whether its whole result list was read, and
        its status for the run report: pages read, offers fetched, the API's
        ``numberRecords`` and why the loop stopped — ``exhausted`` (short
        page), ``total_reached`` (page cap, but every counted offer fetched),
        ``page_cap`` (more remain) or ``error``). The stopping rule and the
        number of requests are unchanged (owner decision D7: no change to the
        EURES request budget); only what the run records is new (B4-D).
        """
        keyword: str = str(query.get("keyword") or "").strip()
        location_codes: list[str] = query.get("location_codes") or []
        raw_jobs_collected: list[dict[str, Any]] = []
        errors: list[str] = []
        status: dict[str, Any] = {"pages": 0, "fetched": 0, "api_total": None, "stop_reason": "page_cap"}

        def _done(reason: str) -> dict[str, Any]:
            status.update(fetched=len(raw_jobs_collected), stop_reason=reason)
            return status

        for page in range(1, self.max_pages_per_query + 1):
            with self._rate_lock:
                self._enforce_rate_limit()
            try:
                raw_jobs, total = self._fetch_page(keyword, location_codes, page)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"EURES: query='{keyword}' loc={location_codes} page={page}: {exc}"
                )
                return raw_jobs_collected, errors, False, _done("error")

            raw_jobs_collected.extend(raw_jobs)
            status.update(pages=page, api_total=total)
            logger.info(
                "EURES: query='%s' loc=%s page=%d/%d fetched=%d total=%s",
                keyword, location_codes, page, self.max_pages_per_query,
                len(raw_jobs), total,
            )

            if len(raw_jobs) < self.results_per_page:
                return raw_jobs_collected, errors, True, _done("exhausted")  # reached last page

        # max_pages_per_query reached: complete only when the API counted no more offers than fetched
        total = status["api_total"]
        if isinstance(total, int) and 0 < total <= len(raw_jobs_collected):
            return raw_jobs_collected, errors, True, _done("total_reached")
        return raw_jobs_collected, errors, False, _done("page_cap")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_page(
        self, keyword: str, location_codes: list[str], page: int
    ) -> tuple[list[dict[str, Any]], int]:
        """POST one search page to the EURES API.

        Returns (jvs list, numberRecords).
        Raises RuntimeError after all retries are exhausted.
        """
        body: dict[str, Any] = {
            "resultsPerPage": self.results_per_page,
            "page": page,
            "sortSearch": self.sort_search,
            "keywords": (
                [{"keyword": keyword, "specificSearchCode": self.keyword_scope}]
                if keyword else []
            ),
            "publicationPeriod": self.publication_period,
            "occupationUris": [],
            "skillUris": [],
            "requiredExperienceCodes": [],
            "positionScheduleCodes": [],
            "sectorCodes": [],
            "educationAndQualificationLevelCodes": [],
            "positionOfferingCodes": [],
            "locationCodes": [c.upper() for c in location_codes],
            "euresFlagCodes": [],
            "otherBenefitsCodes": [],
            "requiredLanguages": [],
            "minNumberPost": None,
            "sessionId": "find-your-job-collector",
            "requestLanguage": "en",
        }
        self._before_request(_API_URL)  # robots.txt and the per-run request cap
        last_error = "unknown error"
        for attempt in range(1, self.retries + 1):
            try:
                logger.debug(
                    "EURES: POST page=%d keyword='%s' loc=%s (attempt %d)",
                    page, keyword, location_codes, attempt,
                )
                resp = httpx.post(_API_URL, json=body, headers=_HEADERS, timeout=self.timeout)
                self._observe_response(resp)

                # 307 here is europa.eu's own overload/maintenance redirect to
                # https://sorry.ec.europa.eu/ (verified 2026-09-08: a genuine
                # EC-side "server temporarily unavailable" page, not bot
                # detection — GETs to the portal succeed fine). Treat it as
                # transient, same as the 5xx codes below.
                if resp.status_code in {307, 408, 429, 500, 502, 503, 504}:
                    last_error = f"HTTP {resp.status_code}"
                    if attempt < self.retries:
                        wait = self._retry_delay(resp, attempt)
                        logger.warning(
                            "EURES: attempt %d got HTTP %d; retrying in %.1fs",
                            attempt, resp.status_code, wait,
                        )
                        time.sleep(wait)
                        continue
                    break

                resp.raise_for_status()
                data = resp.json()
                jvs, total = data.get("jvs") or [], int(data.get("numberRecords") or 0)
                if not jvs and total == 0 and "numberRecords" in data:
                    self._note_empty_result()  # a well-formed answer with no match
                return jvs, total

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    backoff = 2 ** (attempt - 1) * 0.5
                    logger.warning(
                        "EURES: attempt %d network error: %s; retrying in %.1fs",
                        attempt, exc, backoff,
                    )
                    time.sleep(backoff)

        if last_error != "unknown error" and not last_error.startswith("HTTP"):
            self._note_network_error()
        raise RuntimeError(f"EURES API unreachable after {self.retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Pure helpers — testable without network access
# ---------------------------------------------------------------------------

def parse_search_response(text: str) -> list[dict[str, Any]]:
    """The offers in one search API answer, mapped like collect() maps them."""
    data = json.loads(text)
    jvs = data.get("jvs") if isinstance(data, dict) else None
    return [m for m in (_map_job(j) for j in jvs or [] if isinstance(j, dict)) if m.get("title")]


def _map_job(job: dict[str, Any]) -> dict[str, Any]:
    """Map a EURES API jv dict to the pipeline normalizer raw-dict contract."""
    location_map: dict[str, str] = job.get("locationMap") or {}
    iso_codes = list(location_map.keys())
    countries = [_ISO2_TO_COUNTRY.get(code.upper(), code) for code in iso_codes]
    # Use first mapped country as location string (city unavailable in summary).
    location = countries[0] if countries else None

    ts_ms = job.get("creationDate")
    published_at: str | None = None
    if ts_ms:
        try:
            published_at = datetime.fromtimestamp(
                int(ts_ms) / 1000, tz=UTC
            ).isoformat()
        except (ValueError, OSError):
            pass

    job_id = str(job.get("id") or "").strip()
    employer = job.get("employer") or {}
    _raw_company = (employer.get("name") or "").strip()
    # "Non renseigné" is EURES's sentinel for "unknown employer" — store as None.
    company: str | None = None if _raw_company.lower() in ("non renseigné", "non renseigne", "") else _raw_company

    canonical_url: str | None = None
    if job_id:
        canonical_url = f"https://europa.eu/eures/portal/jv-se/jv-details/{job_id}?lang=en"

    return {
        "title": (job.get("title") or "").strip(),
        "company": company,
        "description": (job.get("description") or "").strip(),
        "location": location,
        "countries": countries,
        "canonical_url": canonical_url,
        "source_job_id": job_id or None,
        # The same offer's France Travail id, for cross-source deduplication (#270).
        "ft_offer_id": france_travail_id(job_id),
        "published_at": published_at,
        # positionOfferingCode values (e.g. "directhire") are not mapped to our
        # employment_type vocabulary — too many unknown codes in the wild.
        "type": None,
        # Telework signal is absent from the summary endpoint.
        "remote": False,
        "work_mode": None,
        "skills": [],
    }
