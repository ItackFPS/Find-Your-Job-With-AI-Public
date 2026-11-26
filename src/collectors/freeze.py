"""Temporary collection freeze (owner decision D7, 2026-11-14; ADR 0032).

Every external job collector stays off until it has passed its own validation and the user has
explicitly turned it back on. While a collector is frozen nothing contacts its source: no pipeline
collection, no preflight authentication, no HelloWork scan, scheduled or manual. Stored
offers, saved offers and the application tracker are untouched; collector code and configuration stay
in place for offline tests and replays.

Two conditions turn one collector back on, both required:

1. it is listed in :data:`VALIDATED_COLLECTORS` — shipped with the code, filled in by the change that
   records the collector's passed validation (empty today: no collector is validated yet);
2. the user reactivated it (``POST /api/collection/reactivate``), stored in ``data/settings.json``
   under ``collection_freeze.reactivated``.

An update can only add a collector to the validated list; it never reactivates one, because the second
condition is the user's own stored action. A missing or unreadable ``collection_freeze`` entry reads as
"everything frozen", so a fresh install, an upgraded install and a command-line run are all frozen.

State in ``data/settings.json``::

    "collection_freeze": {"since": "<ISO UTC>", "reactivated": {"<source type>": "<ISO UTC>"}}
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Source type -> where its passed validation is recorded. Empty: no collector is validated yet.
VALIDATED_COLLECTORS: dict[str, str] = {}

# The staged sources scanned outside the pipeline (src/hellowork).
STAGED_SOURCE_TYPES: tuple[str, ...] = ("hellowork",)

# Tests that exercise collection with mocked collectors switch enforcement off (tests/conftest.py);
# the product never changes it.
ENFORCED = True

FROZEN_REASON = (
    "Collection is paused while each source is checked (temporary). Your stored offers, saved offers "
    "and applications are kept. A source comes back only after it passes its checks and you turn it on."
)

_KEY = "collection_freeze"
_lock = threading.Lock()


class CollectionFrozen(RuntimeError):
    """Raised instead of starting a run or scan that would contact a frozen source."""

    def __init__(self, message: str = FROZEN_REASON) -> None:
        super().__init__(message)


class CollectorNotValidated(ValueError):
    """Reactivation asked for a collector that has not passed its validation."""


def _read(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(path: Path | None) -> dict[str, Any]:
    section = _read(path).get(_KEY)
    return section if isinstance(section, dict) else {}


def reactivated(path: Path | None) -> dict[str, str]:
    """Source type -> when the user reactivated it (only string timestamps count)."""
    raw = _section(path).get("reactivated")
    raw = raw if isinstance(raw, dict) else {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def collector_allowed(path: Path | None, source_type: str) -> bool:
    """Whether the collector of ``source_type`` may contact its source now."""
    if not ENFORCED:
        return True
    return source_type in VALIDATED_COLLECTORS and source_type in reactivated(path)


def allowed_types(path: Path | None, source_types: list[str] | set[str] | tuple[str, ...]) -> set[str]:
    return {t for t in source_types if collector_allowed(path, t)}


def configured_collector_types(config_dir: str | Path) -> set[str]:
    """Types of the sources ``sources.yaml`` lists as active (pipeline collectors)."""
    from src.discovery.registry import SourceRegistry

    try:
        registry = SourceRegistry(config_path=str(Path(config_dir) / "sources.yaml"))
    except Exception as exc:  # noqa: BLE001 — reading the config must never raise here
        logger.warning("Collection freeze: could not read sources.yaml: %s", exc)
        return set()
    return {str(s.get("type")) for s in registry.all_sources if s.get("active", True) and s.get("type")}


def pipeline_allowed(path: Path | None, config_dir: str | Path) -> bool:
    """A pipeline run may start only when at least one configured collector may collect: a run with
    every collector frozen would only re-score stored offers and replace the visible list with them."""
    if not ENFORCED:
        return True
    return bool(allowed_types(path, configured_collector_types(config_dir)))


def status(path: Path | None, config_dir: str | Path | None = None) -> dict[str, Any]:
    """What the API and the UI show."""
    section = _section(path)
    types = sorted(configured_collector_types(config_dir) | set(STAGED_SOURCE_TYPES)) if config_dir else []
    react = reactivated(path)
    return {
        "frozen": bool(ENFORCED) and not (config_dir and pipeline_allowed(path, config_dir)),
        "since": section.get("since") if isinstance(section.get("since"), str) else None,
        "reason": FROZEN_REASON,
        "collectors": [
            {"type": t, "validated": t in VALIDATED_COLLECTORS, "reactivated_at": react.get(t),
             "allowed": collector_allowed(path, t)}
            for t in types
        ],
    }


def _write(path: Path, section: dict[str, Any]) -> None:
    data = _read(path)
    data[_KEY] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def record_freeze(path: Path, now: datetime | None = None) -> bool:
    """Store when the freeze started, once (at app start). Returns True when written. Never clears
    a reactivation: an update calling this again changes nothing."""
    with _lock:
        section = _section(path)
        if isinstance(section.get("since"), str):
            return False
        section["since"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        section.setdefault("reactivated", {})
        _write(path, section)
    return True


def reactivate(path: Path, source_type: str, now: datetime | None = None) -> dict[str, Any]:
    """The user's explicit reactivation of one validated collector."""
    if source_type not in VALIDATED_COLLECTORS:
        raise CollectorNotValidated(
            f"{source_type} has not passed its validation yet; it cannot be turned back on.")
    with _lock:
        section = _section(path)
        react = section.get("reactivated")
        react = react if isinstance(react, dict) else {}
        react[source_type] = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        section["reactivated"] = react
        _write(path, section)
    return {"type": source_type, "reactivated_at": react[source_type]}


def deactivate(path: Path, source_type: str) -> None:
    """Freeze one collector again (always allowed)."""
    with _lock:
        section = _section(path)
        react = section.get("reactivated")
        react = react if isinstance(react, dict) else {}
        react.pop(source_type, None)
        section["reactivated"] = react
        _write(path, section)
