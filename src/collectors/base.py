"""Base collector interface.

All collectors inherit from this class and implement the `collect` method.
The base class provides timeout handling, retries, rate limiting, logging, and metrics.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Transient HTTP failures worth retrying: 408/429 are rate-limit/timeout
# signals, 5xx are server-side errors. Any other status (e.g. 404, 401, 403)
# is treated as permanent and fails immediately without retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


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

    ## Provided helpers

      - Timeout handling per request
      - Retry logic with exponential backoff
      - Rate limiting between requests to the same source
      - Structured logging
    """

    def __init__(self, timeout: int = 30, retries: int = 3, rate_limit_delay: float = 1.0):
        self.timeout = timeout
        self.retries = retries
        self.rate_limit_delay = rate_limit_delay
        self._last_request_time: float = 0.0

    @abstractmethod
    def collect(self) -> CollectorResult:
        """Fetch jobs from this source and return normalized data."""
        ...

    # -- helpers -------------------------------------------------------------

    def _enforce_rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            sleep_time = self.rate_limit_delay - elapsed
            logger.debug("Rate limiting: sleeping %.2fs", sleep_time)
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _fetch_with_retry(self, url: str, **kwargs: Any) -> httpx.Response:
        """Fetch a URL with retries and exponential backoff.

        Retries on network errors (timeout/connect) and on transient HTTP
        status codes (408, 429, 500, 502, 503, 504). On a 429/503 response
        with a ``Retry-After`` header, that value is honoured instead of the
        exponential backoff. Any other HTTP error status (e.g. 404, 401) is
        treated as permanent and raised immediately without retrying.
        """
        last_error: str | None = None
        for attempt in range(1, self.retries + 1):
            try:
                logger.debug("Fetching %s (attempt %d/%d)", url, attempt, self.retries)
                response = httpx.get(url, timeout=self.timeout, **kwargs)

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
                if attempt < self.retries:
                    backoff = 2 ** (attempt - 1) * 0.5
                    logger.warning("Attempt %d failed for %s: %s. Retrying in %.1fs",
                                   attempt, url, exc, backoff)
                    time.sleep(backoff)

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
