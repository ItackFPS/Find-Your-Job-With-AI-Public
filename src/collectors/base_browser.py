"""Abstract base class for browser-based collectors (Crawl4AI).

Bridges the synchronous BaseCollector.collect() interface to async Crawl4AI
crawlers without modifying the pipeline orchestrator.

Usage:
    class MyCollector(BaseBrowserCollector):
        async def _async_collect(self) -> CollectorResult:
            self._require_crawl4ai()
            ...

Install Crawl4AI (optional dependency):
    pip install crawl4ai
    playwright install --with-deps chromium
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from typing import Any

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)


class BaseBrowserCollector(BaseCollector):
    """Bridge from sync BaseCollector to async Crawl4AI-based collectors.

    The pipeline orchestrator calls the synchronous collect() which delegates
    to the async _async_collect() via asyncio.run().  No pipeline changes are
    needed to add browser-based sources.

    Concrete subclasses must implement _async_collect().  They should call
    _require_crawl4ai() at the top of that method so a missing installation
    surfaces as a clear error message rather than a bare ImportError.

    Rate-limiting is async-aware: use _async_sleep_rate_limit() between page
    requests instead of the parent's _enforce_rate_limit().
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        # Browser collectors are slower and more expensive than HTTP collectors.
        # Default timeout and rate limit are higher accordingly.
        super().__init__(
            timeout=int(self.config.get("timeout", 60)),
            retries=int(self.config.get("retries", 2)),
            rate_limit_delay=float(self.config.get("rate_limit_delay", 3.0)),
        )

    def collect(self) -> CollectorResult:
        """Sync entry point: runs the async collector via asyncio.run()."""
        return asyncio.run(self._async_collect())

    @abstractmethod
    async def _async_collect(self) -> CollectorResult:
        """Override in subclasses to implement browser-based collection."""
        ...

    async def _async_sleep_rate_limit(self) -> None:
        """Async-safe rate-limit sleep between page requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    @staticmethod
    def _require_crawl4ai() -> None:
        """Raise ImportError with install instructions when crawl4ai is absent."""
        try:
            import crawl4ai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "crawl4ai is required for browser-based collectors.\n"
                "Install it with:\n"
                "  pip install 'find-your-job-with-ai[browser]'\n"
                "  playwright install --with-deps chromium\n"
                "Or directly:\n"
                "  pip install crawl4ai && playwright install --with-deps chromium"
            ) from exc
