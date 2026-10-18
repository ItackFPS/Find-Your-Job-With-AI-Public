"""Advisory lock preventing two reconciliation runs (the automatic worker's
tick and a Claude-triggered on-demand run, or two overlapping ticks) from
racing on the same registry file at once.

Plain exclusive-file-creation lock — no OS-specific locking (flock/msvcrt)
needed, since a reconciliation pass is a short, bounded operation and a
false "already running" is harmless (the caller just retries next tick).
Mirrors the advisory-lock pattern already used elsewhere in this project
(.claude/scheduled_tasks.lock).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

# A lock older than this is assumed to be left over from a crashed/killed
# process rather than a genuinely long-running reconciliation, and is
# reclaimed rather than blocking forever.
_STALE_LOCK_SECONDS = 10 * 60

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def default_lock_path() -> Path:
    """The one lock path both the automatic worker (src.notifications.
    discord_reconciler_worker) and Claude's on-demand MCP tool
    (src.discord_admin.mcp_server) use by default.

    Anchored at the project root via __file__ rather than a bare relative
    "data/..." path — the worker runs inside the uvicorn backend process
    (normally started from the repo root) while the MCP server runs as its
    own subprocess launched by Claude Code, which may have a different
    working directory. If each computed a *relative* default, they could
    silently resolve to two different physical files, defeating the
    cross-process lock entirely. Anchoring both at __file__ guarantees they
    always agree regardless of either process's cwd.
    """
    return _PROJECT_ROOT / "data" / "discord_reconciliation.lock"


class ReconciliationInProgressError(Exception):
    """Another reconciliation run holds the lock right now."""


@contextlib.contextmanager
def reconciliation_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0.0
        if age < _STALE_LOCK_SECONDS:
            raise ReconciliationInProgressError(
                f"Reconciliation already in progress (lock held {age:.0f}s ago)"
            )
        # Stale — a prior run crashed without cleaning up. Reclaim it.
        lock_path.unlink(missing_ok=True)

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ReconciliationInProgressError("Reconciliation already in progress") from exc

    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "started_at": time.time()}, f)

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)
