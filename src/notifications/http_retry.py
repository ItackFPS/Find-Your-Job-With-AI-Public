"""Bounded retry/backoff for outbound notification HTTP calls.

The ONLY retry policy in the notification system — shared by
DiscordProvider (src/notifications/discord.py) and N8nProvider
(src/notifications/n8n.py) so neither re-implements it.

Retries:
  - HTTP 429 — respects the response's Retry-After header, capped at
    RETRY_AFTER_CAP_SECONDS.
  - HTTP 5xx.
  - httpx.ConnectError / httpx.TimeoutException.
Never retries any other 4xx (a permanently broken webhook URL or malformed
payload should fail fast, not waste the retry budget repeating the same
mistake).

Bounded by design: MAX_RETRIES total retries (MAX_RETRIES + 1 attempts),
and RETRY_AFTER_CAP_SECONDS ceiling on any single sleep. This keeps the
worst-case added latency per notification call small and predictable —
notification sends run inline on PipelineRunner's background thread (see
src/notifications/engine.py's module docstring) and must never meaningfully
delay the next pipeline run's ability to start (single-run-at-a-time).

This module only decides WHEN to retry — it never changes what "success"
or "failure" means to a caller. send_with_retry() returns the last HTTP
response as-is (2xx, a non-retryable 4xx, or a still-failing 429/5xx after
retries are exhausted) so callers keep calling response.raise_for_status()
exactly as before. A still-failing connection/timeout error after the last
attempt re-raises the original httpx exception unchanged, so callers' own
`except Exception` blocks keep working with zero changes to their control
flow — this module adds retries, not a new failure model.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 2                      # -> up to 3 total attempts
RETRY_AFTER_CAP_SECONDS = 5.0        # never sleep longer than this even if a 429 asks for more
_BACKOFF_SCHEDULE = (1.0, 3.0)       # seconds, before retry 1 and retry 2 respectively


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), RETRY_AFTER_CAP_SECONDS)
    except ValueError:
        return None


def _backoff_delay(attempt: int) -> float:
    return _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]


def send_with_retry(request: Callable[[], httpx.Response]) -> httpx.Response:
    """Call ``request()`` up to MAX_RETRIES + 1 times total.

    ``request`` must perform exactly one HTTP call and return the raw
    response WITHOUT calling ``raise_for_status()`` itself — this function
    inspects ``status_code`` directly so it can decide whether to retry
    before any exception is raised. The final response (successful,
    non-retryable, or retries-exhausted) is returned as-is for the caller
    to handle exactly as it already does today.
    """
    attempt = 0
    while True:
        try:
            response = request()
        except (httpx.ConnectError, httpx.TimeoutException):
            if attempt >= MAX_RETRIES:
                raise
            delay = _backoff_delay(attempt)
            logger.debug(
                "Notification HTTP: connection/timeout error, retry %d/%d after %.1fs",
                attempt + 1, MAX_RETRIES, delay,
            )
            time.sleep(delay)
            attempt += 1
            continue

        if attempt >= MAX_RETRIES or not _is_retryable_status(response.status_code):
            return response

        delay = _backoff_delay(attempt)
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                delay = retry_after
        logger.debug(
            "Notification HTTP: status %d, retry %d/%d after %.1fs",
            response.status_code, attempt + 1, MAX_RETRIES, delay,
        )
        time.sleep(delay)
        attempt += 1
