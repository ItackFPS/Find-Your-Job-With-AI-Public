"""In-process SSE progress broadcasting for pipeline runs.

Purely observational: fans out best-effort progress events derived from the
same stats the pipeline orchestrator already produces (see the
``progress_callback`` closure in ``PipelineRunner._execute``,
src/api/runner.py) to connected Server-Sent Events subscribers. A publish
failure (no loop bound yet, a full queue, a broken subscriber) is always
swallowed here and must never affect pipeline execution.

Single-process assumption: the app runs as a single uvicorn worker (see the
src/api/app.py module docstring) with the pipeline running as a background
thread inside that process, so a single in-memory asyncio.Queue fan-out is
correct here — no cross-process broker is needed. If the app ever moves to
multiple worker processes, this module would need to change accordingly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 50


class ProgressBus:
    """Fan-out of pipeline progress events to connected SSE subscribers.

    ``_subscribers`` is only ever touched on the bound asyncio event loop
    thread (subscribe/unsubscribe run inside the SSE endpoint coroutine;
    ``_dispatch`` is scheduled onto that same loop via
    ``call_soon_threadsafe``), so no additional lock is needed. ``publish``
    is the only method meant to be called from another thread (the pipeline
    background thread).
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the running asyncio loop. Call once from the app's lifespan."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        """Best-effort publish. Safe to call from any thread; never raises."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("progress bus publish skipped: %s", exc)

    def _dispatch(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest queued frame so a slow subscriber doesn't
                # fall further behind — losing an intermediate progress
                # frame is fine, the next one supersedes it.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("progress bus dispatch to subscriber failed: %s", exc)
