"""Indeed job collector through python-jobspy (update plan F6.3).

Indeed has no public API (the Publisher API ended). Every open tool reads the mobile
GraphQL endpoint of Indeed's iOS app, and python-jobspy (MIT licence, already a
dependency of the LinkedIn scanner) ships the client and the app key. Its maintainers
track key and schema changes, so this project handles no Indeed credential and the user
configures nothing.

Access class ``unofficial``, opt-in (plan rule I1): the preflight skips Indeed unless the
user turned it on (``src.app_settings.unofficial_source_enabled``).

Query strategy:
  One ``jobspy.scrape_jobs`` call per configured query (keyword + location) and per
  ``discovery_queries`` keyword from the orchestrator (run against ``default_location``).
  jobspy pages through the results itself (100 per page) with no pause, so the rate
  policy paces the *queries*, and the page budget is counted from ``results_wanted``
  before each call.

Mapping: ids and canonical URLs stay the ones the former GraphQL client produced
(``fr.indeed.com/viewjob?jk=<key>``), so offers stored before the switch deduplicate.
jobspy reports "fully remote" but not "hybrid", and gives a city rather than a postal
code. The work mode is then left unknown (never assumed on-site), and the place resolver
and remote extractor (F4.1/F4.2) read the location and the description.

Indeed Terms of Service: automated access is prohibited. Personal, local use only.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, cast

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture, RequestBudgetExceeded

logger = logging.getLogger(__name__)

_JOBS_PER_PAGE = 100            # jobspy's Indeed page size (jobspy/indeed/constant.py)
_COUNTRY = "france"
_API_ORIGIN = "https://apis.indeed.com/graphql"

# jobspy job types -> canonical pipeline values (the former attribute map's outputs).
_JOB_TYPE_MAP: dict[str, str] = {
    "fulltime": "full-time",
    "parttime": "part-time",
    "internship": "internship",
    "contract": "contract",
    "temporary": "contract",
}

# jobspy's country names (Country enum values) -> ISO codes, for the countries we meet.
_COUNTRY_CODES: dict[str, str] = {
    "france": "FR", "belgium": "BE", "switzerland": "CH", "luxembourg": "LU", "canada": "CA",
    "germany": "DE", "spain": "ES", "italy": "IT", "netherlands": "NL", "uk": "GB", "usa": "US",
}

ScrapeFn = Callable[..., Any]

# jobspy's Indeed loggers: "JobSpy:Indeed" (1.3.x, the version the installer pins) and
# "jobspy.indeed" (1.4.x). 1.3.x reports a failed page only by logging an ERROR; 1.4.x also
# fills DataFrame.attrs["errors"].
_JOBSPY_LOGGERS = ("JobSpy:Indeed", "jobspy.indeed")
# "response status code 403", "Indeed response status code 429": Indeed answered.
_STATUS = re.compile(r"status code (\d{3})")


class _ErrorCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _jobspy_errors() -> Iterator[list[str]]:
    """ERROR messages jobspy's Indeed scraper logs during the block (the same pattern as
    src/linkedin/scanner.py::_jobspy_errors)."""
    collector = _ErrorCollector()
    loggers = [logging.getLogger(name) for name in _JOBSPY_LOGGERS]
    for lg in loggers:
        lg.addHandler(collector)
    try:
        yield collector.messages
    finally:
        for lg in loggers:
            lg.removeHandler(collector)


class _Answer:
    """What _observe_response reads, for an answer jobspy received on our behalf."""

    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


def _default_scrape() -> ScrapeFn:
    from jobspy import scrape_jobs  # type: ignore[import-untyped]  # lazy: pandas is heavy

    return cast(ScrapeFn, scrape_jobs)


class IndeedCollector(BaseCollector):
    """Collects job listings from Indeed through python-jobspy.

    Configuration (sources.yaml -> config sub-key):
        queries             List of {keyword, location} dicts.
        discovery_queries   Keyword strings injected by the orchestrator; each is run
                            against default_location.
        default_location    Location for discovery_queries (default: "France").
        max_pages_per_query Pages of 100 per query (default: 5).
        max_jobs_per_query  Hard cap per query (default: max_pages_per_query x 100).
        date_filter_hours   Only jobs posted in the last N hours (default: 720 = 30 days).
        rate_limit_delay    Seconds from the end of one query to the start of the next
                            (floor: rate_policy.min_delay_seconds).

    jobspy uses its own request timeout and retries; this collector sets neither.
    """

    # unofficial: the backend of Indeed's iOS app, read through python-jobspy. Opt-in.
    access_class = "unofficial"
    # jobspy fetches a query's pages back to back; queries are spaced by the delay, and
    # every page a query returned counts against the per-run cap.
    rate_policy = RatePolicy(min_delay_seconds=2.0, max_requests_per_run=500)

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("indeed_jobspy.json", self._load_fixture("indeed_jobspy.json"), parse_jobspy_rows)

    def __init__(self, config: dict[str, Any] | None = None, scrape: ScrapeFn | None = None):
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 20)),
            retries=int(cfg.get("retries", 3)),
            rate_limit_delay=float(cfg.get("rate_limit_delay", 2.0)),
        )
        self.queries: list[dict[str, str]] = list(cfg.get("queries") or [])
        self.discovery_queries: list[str] = list(cfg.get("discovery_queries") or [])
        self.default_location: str = str(cfg.get("default_location", "France"))
        self.max_pages_per_query: int = max(1, int(cfg.get("max_pages_per_query", 5)))
        cap = self.max_pages_per_query * _JOBS_PER_PAGE
        self.max_jobs_per_query: int = min(int(cfg["max_jobs_per_query"]), cap) if cfg.get("max_jobs_per_query") else cap
        self.date_filter_hours: int | None = int(cfg["date_filter_hours"]) if cfg.get("date_filter_hours") else None
        self._scrape = scrape

    def collect(self) -> CollectorResult:
        start_time = time.time()
        all_jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()

        effective: list[tuple[str, str]] = []
        for q in self.queries:
            kw = str(q.get("keyword") or "").strip()
            if kw:
                effective.append((kw, str(q.get("location") or self.default_location).strip()))
        for kw in self.discovery_queries:
            if str(kw).strip():
                effective.append((str(kw).strip(), self.default_location))

        if not effective:
            logger.warning("IndeedCollector: no queries configured — nothing to collect")
            return CollectorResult(source_name="indeed", jobs=[], errors=["No queries configured"],
                                   duration_seconds=0.0)

        try:
            scrape = self._scrape or _default_scrape()
        except ImportError:
            return CollectorResult(source_name="indeed", jobs=[], duration_seconds=0.0,
                                   errors=["indeed: python-jobspy is not installed"])

        for keyword, location in effective:
            try:
                rows, error = self._collect_query(scrape, keyword, location)
            except RequestBudgetExceeded as exc:
                errors.append(f"indeed: {exc}")
                break
            if error:
                errors.append(f"indeed [{keyword} @ {location}]: {error}")
            for raw in rows:
                key = raw.get("source_job_id")
                if key and key not in seen:
                    seen.add(key)
                    all_jobs.append(raw)

        duration = time.time() - start_time
        self._log_collection("indeed", len(all_jobs), errors, duration)
        return CollectorResult(source_name="indeed", jobs=all_jobs, errors=errors, duration_seconds=duration)

    def _collect_query(self, scrape: ScrapeFn, keyword: str, location: str) -> tuple[list[dict[str, Any]], str | None]:
        """One jobspy search. Returns ``(raw jobs, error or None)``; never raises except
        ``RequestBudgetExceeded`` (the run's page budget is spent).

        Budget: the first page is charged before the call, the further pages the query
        returned (one per 100 rows) after it. Spacing: the delay runs from the end of the
        previous query, since a query's pages can take several seconds."""
        self._before_request(_API_ORIGIN, check_robots=False)
        self._enforce_rate_limit()
        with _jobspy_errors() as logged:
            try:
                df = scrape(
                    site_name=["indeed"], search_term=keyword, location=location, country_indeed=_COUNTRY,
                    results_wanted=self.max_jobs_per_query, hours_old=self.date_filter_hours,
                    description_format="html", verbose=0,
                )
            except Exception as exc:  # noqa: BLE001 — one failed query must not stop the others
                df = None
                logged.append(str(exc))
        self._last_request_time = (type(self)._clock or time.time)()
        attrs = getattr(df, "attrs", None) or {}
        reported = (attrs.get("errors") or {}).get("indeed")
        errors = [str(reported)] if reported else []
        errors += [m for m in logged if m not in errors]
        records = df.to_dict("records") if df is not None and hasattr(df, "to_dict") else []
        records = [r for r in records if isinstance(r, dict)]
        for _ in range(max(0, math.ceil(len(records) / _JOBS_PER_PAGE) - 1)):
            self._before_request(_API_ORIGIN, check_robots=False)
        rows = [_map_row(r) for r in records]
        rows = [r for r in rows if r["source_job_id"] and r["title"]]
        if errors:
            self._note_failure(errors[0])
            logger.warning("IndeedCollector: query %r: %s", keyword, errors[0])
            return rows, errors[0]
        if records:
            # Indeed answered with offers: an answer for the parser canary, which then
            # speaks up if none of them could be read (a changed row format).
            self._observe_response(_Answer(200, json.dumps(records, default=str)))
        else:
            self._observe_response(_Answer(200), parsed=False)
            self._note_empty_result()
        return rows, None

    def _note_failure(self, message: str) -> None:
        """A failed query: an HTTP status in the message means Indeed answered (blocked, rate
        limited); anything else is counted as no answer at all (DNS, proxy, timeout)."""
        m = _STATUS.search(message)
        if m:
            self._observe_response(_Answer(int(m.group(1))), parsed=False)
        else:
            self._note_network_error()


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(n) else n


def _published_at(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()
    text = _text(value)
    if not text:
        return None
    try:
        return _published_at(date.fromisoformat(text[:10]))
    except ValueError:
        return None


def _map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map one jobspy row (``JobPost.model_dump()``) to the pipeline normalizer contract.

    Pure function: no I/O. ``source_job_id`` drops jobspy's ``in-`` prefix so ids match
    the offers the former GraphQL client stored."""
    key = _text(row.get("id"))
    key = key[3:] if key.startswith("in-") else key
    location = _text(row.get("location"))
    country = _text(location.rsplit(",", 1)[-1]).lower() if location else ""
    code = _COUNTRY_CODES.get(country)
    remote = row.get("is_remote") is True
    job_types = [t.strip() for t in _text(row.get("job_type")).split(",") if t.strip()]
    employment = next((_JOB_TYPE_MAP[t] for t in job_types if t in _JOB_TYPE_MAP), None)
    return {
        "source_job_id": key or None,
        "title": _text(row.get("title")),
        "company": _text(row.get("company")) or None,
        "description": _text(row.get("description")),
        "location": location,
        "countries": [code] if code else [],
        "department_code": None,       # the place resolver reads the city (F4.1)
        "work_mode": "remote" if remote else None,
        "remote": remote,
        "type": employment,
        "salary_min": _number(row.get("min_amount")),
        "salary_max": _number(row.get("max_amount")),
        "currency": _text(row.get("currency")) or "EUR",
        "published_at": _published_at(row.get("date_posted")),
        "canonical_url": _text(row.get("job_url")) or None,
        "apply_url": _text(row.get("job_url_direct")) or None,
        "company_url": _text(row.get("company_url")) or None,
        "skills": [],
    }


def parse_jobspy_rows(text: str) -> list[dict[str, Any]]:
    """The offers in a recorded jobspy result (a JSON list of rows), mapped like collect()."""
    data = json.loads(text)
    rows = [_map_row(r) for r in (data if isinstance(data, list) else []) if isinstance(r, dict)]
    return [r for r in rows if r["source_job_id"] and r["title"]]
