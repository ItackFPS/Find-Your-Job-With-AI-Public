"""Base collector interface.

All collectors inherit from this class and implement the `collect` method.
The base class provides timeout handling, retries, rate limiting, logging, and metrics.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel

from src.collectors.robots import RobotsCache, origin

logger = logging.getLogger(__name__)

# Transient HTTP failures worth retrying: 408/429 are rate-limit/timeout
# signals, 5xx are server-side errors. Any other status (e.g. 404, 401, 403)
# is treated as permanent and fails immediately without retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# -- source plug-in contract (plan §C1, §E2 item 3, F6.1) --------------------------------

# How a collector reaches its source. ``blocked`` (CAPTCHA, WAF or login) is never shipped.
#   official     documented API or feed meant for reuse                          (on by default)
#   public_page  anonymous pages robots.txt allows; robots.txt is checked live    (on by default)
#   unofficial   anonymous endpoints a site's own apps use, not meant for reuse   (opt-in, F6.5)
AccessClass = Literal["official", "public_page", "unofficial"]
ACCESS_CLASSES: tuple[str, ...] = ("official", "public_page", "unofficial")

DEFAULT_ROBOTS_AGENT = "FindYourJobWithAI"
DEFAULT_ROBOTS_USER_AGENT = ("Mozilla/5.0 (compatible; FindYourJobWithAI/2.0; "
                             "+https://github.com/ItackFPS/Find-Your-Job-With-AI-Public)")
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class RatePolicy:
    """How hard a collector may hit its source.

    ``min_delay_seconds`` is a floor: a lower configured ``rate_limit_delay`` is raised
    to it. ``max_requests_per_run`` caps the requests one ``collect()`` makes (``None``:
    no cap); past it, every further request is refused with ``RequestBudgetExceeded``.
    """

    min_delay_seconds: float = 0.0
    max_requests_per_run: int | None = None


@dataclass(frozen=True)
class RecordedFixture:
    """A recorded (trimmed or synthetic) page or response of a source, for the parser
    canary: ``parse(content)`` must return at least one raw job while the parser still
    matches the source's format."""

    name: str
    content: str
    parse: Callable[[str], list[dict[str, Any]]]


class RobotsDisallowed(RuntimeError):
    """A public-page collector was about to request a URL robots.txt does not allow."""


class RequestBudgetExceeded(RuntimeError):
    """A collector reached its ``rate_policy.max_requests_per_run``."""


class CollectorResult(BaseModel):
    """Result of a collection run from a single source."""

    source_name: str
    jobs: list[dict[str, Any]]
    errors: list[str] = []
    duration_seconds: float = 0.0
    # Optional structured metadata (e.g. per-query stats). Not used by the
    # pipeline core; callers may read it for diagnostics.
    metadata: dict[str, Any] = {}


class BaseCollector(ABC):
    """Abstract base class for all job collectors.

    ## Collector contract

    A collector's only responsibility is to **fetch raw job data** from one
    external source and return it as a list of plain dictionaries.

    The pipeline enforces this flow on every run:

        Collector.collect()          →  CollectorResult.jobs (raw dicts)
        normalize_jobs(source, jobs) →  list[Job]  (content_hash set here)
        deduplicate / filter / score →  ...

    **Rules that every collector must follow:**

    1. ``collect()`` returns **raw data only** — do not construct ``Job``
       instances inside a collector.  The normalizer is the single place that
       maps source-specific fields to the unified model and generates
       ``content_hash``.

    2. Return ``CollectorResult`` even on partial failure.  Append human-readable
       messages to ``CollectorResult.errors``; do not raise.

    3. Network errors, HTTP errors, malformed responses, and timeouts must be
       caught and turned into error strings.  One failing source must never
       stop the rest of the pipeline.

    4. Do not write to the database.  Storage is handled by the pipeline
       orchestrator after normalization and deduplication.

    5. Do not apply filtering, scoring, or any business logic.  Fetch and
       return — nothing else.

    ## Source plug-in contract (plan F6.1)

    Every collector declares, as class attributes:

      - ``access_class``: ``official`` | ``public_page`` | ``unofficial`` (see
        ``AccessClass``), with a comment saying why.
      - ``robots_policy``: whether every URL is checked against the site's robots.txt
        before it is requested (always on for ``public_page``, whatever is declared).
      - ``rate_policy``: a ``RatePolicy`` (delay floor, request cap per run).

    and may override ``fixture()`` (a recorded page for the parser canary, required for
    collectors that scrape pages or app endpoints). ``health()`` reports what one run
    did. Requests made through ``_fetch_with_retry`` (or announced with
    ``_before_request``) are checked against robots.txt and the request cap; a refused
    request raises ``RobotsDisallowed`` / ``RequestBudgetExceeded`` (both
    ``RuntimeError``), which ``collect()`` turns into an error string like any failure.

    ## Provided helpers

      - Timeout handling per request
      - Retry logic with exponential backoff
      - Rate limiting between requests to the same source
      - robots.txt checks (RFC 9309: 4xx allows, 5xx or unreachable disallows) and a
        per-run request cap
      - Structured logging
    """

    access_class: ClassVar[AccessClass | None] = None
    robots_policy: ClassVar[bool] = False
    rate_policy: ClassVar[RatePolicy] = RatePolicy()
    # Product token matched against robots.txt groups, and the User-Agent robots.txt is
    # fetched with (a collector sending its own User-Agent sets both to match it).
    robots_agent: ClassVar[str] = DEFAULT_ROBOTS_AGENT
    robots_user_agent: ClassVar[str] = DEFAULT_ROBOTS_USER_AGENT
    # robots.txt answers read per RFC 9309 (4xx: no restriction; 5xx or unreachable:
    # disallow all). True: only a served, non-empty robots.txt allows anything.
    robots_strict: ClassVar[bool] = False
    # The parser canary only speaks up when a 200 answer was at least this large.
    canary_min_bytes: ClassVar[int] = 2048
    # Injected clock and sleep for the rate limiter (None: time.time / time.sleep, looked
    # up at call time). Tests set them on BaseCollector so no collector really waits.
    _clock: ClassVar[Callable[[], float] | None] = None
    _sleep: ClassVar[Callable[[float], None] | None] = None

    def __init__(self, timeout: int = 30, retries: int = 3, rate_limit_delay: float = 1.0):
        self.timeout = timeout
        self.retries = retries
        self.rate_limit_delay = max(float(rate_limit_delay), self.rate_policy.min_delay_seconds)
        self._last_request_time: float = 0.0
        self._requests_made = 0
        self._request_cap_reached = False
        self._robots_refused = 0
        self._responses = 0
        self._network_errors = 0
        self._empty_results = 0
        self._ok_responses = 0
        self._largest_ok_bytes = 0
        self._last_status: int | None = None
        self._budget_lock = threading.Lock()
        self._robots: RobotsCache | None = None

    @abstractmethod
    def collect(self) -> CollectorResult:
        """Fetch jobs from this source and return normalized data."""
        ...

    # -- contract ------------------------------------------------------------

    @property
    def honours_robots(self) -> bool:
        return self.robots_policy or self.access_class == "public_page"

    def robots_check(self, url: str) -> bool:
        """Whether ``url`` may be requested: always for a collector without a robots
        policy; otherwise what the site's robots.txt says (fetched once per run; a 4xx
        answer allows everything, a 5xx answer or an unreachable site nothing, and with
        ``robots_strict`` only a served, non-empty robots.txt allows anything)."""
        if not self.honours_robots:
            return True
        return self._robots_cache().allows(url)

    def fixture(self) -> RecordedFixture | None:
        """A recorded page of this source for the parser canary; ``None`` when the
        collector reads a documented API (an empty answer there can be genuine)."""
        return None

    def health(self) -> dict[str, Any]:
        """What the last ``collect()`` did, for the run's per-source row and the canary.

        Safe on a subclass that never called ``BaseCollector.__init__`` (all zeros)."""
        def get(name: str, default: Any = 0) -> Any:
            return getattr(self, name, default)

        return {
            "access_class": self.access_class,
            "requests": get("_requests_made"),
            "request_cap": self.rate_policy.max_requests_per_run,
            "request_cap_reached": get("_request_cap_reached", False),
            "robots_refused": get("_robots_refused"),
            # any HTTP answer at all, and requests that got none (DNS, connect, timeout)
            "responses": get("_responses"),
            "network_errors": get("_network_errors"),
            # answers the collector recognised as a genuine "no offer" result
            "empty_results": get("_empty_results"),
            # HTTP 200 answers of pages parsed for offers (not robots.txt or sitemaps)
            "ok_responses": get("_ok_responses"),
            "largest_ok_bytes": get("_largest_ok_bytes"),
            "last_status": get("_last_status", None),
        }

    @staticmethod
    def _load_fixture(filename: str) -> str:
        return (FIXTURES_DIR / filename).read_text(encoding="utf-8")

    # -- helpers -------------------------------------------------------------

    def _robots_cache(self) -> RobotsCache:
        if self._robots is None:
            self._robots = RobotsCache(self.robots_agent, self.robots_user_agent, timeout=min(self.timeout, 15),
                                       strict=self.robots_strict)
        return self._robots

    def _before_request(self, url: str, *, check_robots: bool = True) -> None:
        """Gate one request (not its retries): robots.txt, then the per-run cap.

        Raises ``RobotsDisallowed`` or ``RequestBudgetExceeded``; a refused URL is not
        counted.
        """
        if check_robots and self.honours_robots and not url.endswith("/robots.txt"):
            cache = self._robots_cache()
            if not cache.allows(url):
                with self._budget_lock:
                    self._robots_refused += 1
                    if origin(url) in cache.unreachable:
                        self._network_errors += 1
                raise RobotsDisallowed(f"not requested: {cache.refusal(url)}")
        cap = self.rate_policy.max_requests_per_run
        with self._budget_lock:
            if cap is not None and self._requests_made >= cap:
                self._request_cap_reached = True
                raise RequestBudgetExceeded(
                    f"request cap of {cap} per run reached; not requested: {origin(url)}")
            self._requests_made += 1

    def _note_network_error(self) -> None:
        """A request got no HTTP answer at all (DNS, connection refused, timeout)."""
        with self._budget_lock:
            self._network_errors += 1

    def _note_empty_result(self) -> None:
        """A page or answer was read and genuinely lists no offer (e.g. an empty result
        list in a well-formed answer): the parser canary then stays quiet."""
        with self._budget_lock:
            self._empty_results += 1

    def _observe_response(self, response: Any, *, parsed: bool = True) -> None:
        """Record a response for ``health()`` and the parser canary. ``parsed=False``
        for answers that are not parsed for offers (robots.txt, sitemaps, taxonomy
        indexes): they count as reachability, never as "pages served" for the canary."""
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            return
        with self._budget_lock:
            self._responses += 1
        if not parsed:
            return
        size = 0
        content = getattr(response, "content", None)
        if isinstance(content, bytes | bytearray):
            size = len(content)
        else:
            text = getattr(response, "text", None)
            if isinstance(text, str):
                size = len(text.encode("utf-8", "replace"))
        with self._budget_lock:
            self._last_status = status
            if status == 200:
                self._ok_responses += 1
                self._largest_ok_bytes = max(self._largest_ok_bytes, size)

    def _enforce_rate_limit(self) -> None:
        # Read from the class: a function stored on a class would bind to the instance.
        clock = type(self)._clock or time.time
        sleep = type(self)._sleep or time.sleep
        elapsed = clock() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            sleep_time = self.rate_limit_delay - elapsed
            logger.debug("Rate limiting: sleeping %.2fs", sleep_time)
            sleep(sleep_time)
        self._last_request_time = clock()

    def _fetch_with_retry(self, url: str, *, parsed: bool = True, **kwargs: Any) -> httpx.Response:
        """Fetch a URL with retries and exponential backoff.

        Retries on network errors (timeout/connect) and on transient HTTP
        status codes (408, 429, 500, 502, 503, 504). On a 429/503 response
        with a ``Retry-After`` header, that value is honoured instead of the
        exponential backoff. Any other HTTP error status (e.g. 404, 401) is
        treated as permanent and raised immediately without retrying.

        The URL is first checked against robots.txt (public-page collectors) and the
        per-run request cap: see ``_before_request``. ``parsed=False`` for a page that is
        not parsed for offers (see ``_observe_response``).
        """
        self._before_request(url)
        last_error: str | None = None
        network_failure = False
        for attempt in range(1, self.retries + 1):
            try:
                logger.debug("Fetching %s (attempt %d/%d)", url, attempt, self.retries)
                response = httpx.get(url, timeout=self.timeout, **kwargs)
                self._observe_response(response, parsed=parsed)
                network_failure = False

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    if attempt < self.retries:
                        backoff = self._retry_delay(response, attempt)
                        logger.warning(
                            "Attempt %d failed for %s: HTTP %d. Retrying in %.1fs",
                            attempt, url, response.status_code, backoff,
                        )
                        time.sleep(backoff)
                        continue
                    break

                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = str(exc)
                network_failure = True
                if attempt < self.retries:
                    backoff = 2 ** (attempt - 1) * 0.5
                    logger.warning("Attempt %d failed for %s: %s. Retrying in %.1fs",
                                   attempt, url, exc, backoff)
                    time.sleep(backoff)

        if network_failure:
            self._note_network_error()
        raise RuntimeError(f"Failed to fetch {url} after {self.retries} attempts: {last_error}")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Return the wait time before the next retry.

        Honours a numeric ``Retry-After`` header (seconds) when present;
        otherwise falls back to exponential backoff.
        """
        retry_after = response.headers.get("Retry-After", "")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        backoff: float = 2 ** (attempt - 1) * 0.5
        return backoff

    def _log_collection(self, source_name: str, jobs_count: int, errors: list[str], duration: float) -> None:
        logger.info(
            "Collection complete | source=%s | fetched=%d | rejected=%d | duration=%.2fs",
            source_name, jobs_count, len(errors), duration,
        )
