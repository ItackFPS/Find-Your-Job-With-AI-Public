"""Durable file logging for the backend process.

Configured at import time from ``src/api/app.py`` — before uvicorn does its
own ``logging.config.dictConfig`` — so it works no matter how the process is
launched (production ``uvicorn src.api.app:app --port 8000`` via
scripts/Start_backend.bat, or a manual ``--reload`` dev run). Attaches to the
ROOT logger; uvicorn's default logging config uses
``disable_existing_loggers: False`` and only touches its own named loggers,
so this handler survives uvicorn's setup and every ``src.*`` module logger
(which all propagate to root by default) ends up captured here too.

This was added after an incident where a manually-started ``--reload`` dev
server silently discarded all backend/scanner log output (nothing survives a
`--reload` worker restart or a Task-Scheduler-managed run without this), which
made a several-hour scanner interruption impossible to diagnose after the
fact. See docs/incidents or the corresponding investigation for details.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False


def configure_logging(log_dir: str | Path = "logs/backend") -> Path:
    """Attach a rotating file handler (+ console) to the root logger.

    Idempotent within a single process. Returns the log file path.
    """
    global _configured

    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_file = log_dir_path / f"backend-{datetime.now(UTC).date().isoformat()}.log"

    if _configured:
        return log_file

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [pid=%(process)d] %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # httpx (and httpx's own transport, httpcore) log the full request URL
    # at INFO level by default -- including query strings and, critically,
    # any webhook/token embedded directly in the URL path (e.g. Discord
    # webhook URLs, which ARE the credential). WARNING silences that
    # per-request line while still surfacing real connection problems.
    # Discovered live during this change's own verification: a real Discord
    # webhook URL showed up in the log file the first time a notification
    # fired during a test run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
    return log_file
