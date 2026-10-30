"""Application settings that are not part of the job-search profile (update plan F2.4).

Stored in ``data/settings.json`` (never in ``config/profile.yaml``: the profile is what searches are
built from, and changing it creates a new strategy version). Today it holds the desktop
notification categories the user opted into; the tray app reads them through
``GET /api/settings/notifications`` before each Windows notification.

It also holds the user's choice for each *unofficial* source (``unofficial_sources``, plan rule
I1, F6.3/F6.4: Indeed, LinkedIn, HelloWork): off on a fresh install. Until the consent screen
exists (F6.5), an install that already set the source up the old way (``INDEED_API_KEY``) or
already ran a LinkedIn/HelloWork scan counts as having chosen it.

And the background collection (``background``, issue #80, owner decision R-Q3): ``enabled``
(default on) stops every automatic scan and search when false; ``daily_time`` ("HH:MM", local
time, default 08:00) is when each day's automatic runs become due (src/scheduler/service.py).

Categories (default on: the two a user would not want to miss):

=================  =======  ==========================================================================
category           default  fires when
=================  =======  ==========================================================================
strong_matches     on       a search finishes with offers in a "strong" tier or better
search_failed      on       a search (pipeline run) or a LinkedIn/HelloWork scan fails
source_paused      off      a LinkedIn/HelloWork scan pauses, or stops on a rate limit
saved_expiring     off      an offer you saved expires tomorrow — not available yet: offers carry no
                            saved flag or expiry date. TODO(F6.6): fire it once saved offers and their
                            closing dates are stored.
=================  =======  ==========================================================================
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

NOTIFICATION_DEFAULTS: dict[str, bool] = {
    "strong_matches": True,
    "search_failed": True,
    "source_paused": False,
    "saved_expiring": False,
}
# Categories the app can actually fire today (the UI shows the others as "coming").
NOTIFICATION_AVAILABLE: dict[str, bool] = {
    "strong_matches": True,
    "search_failed": True,
    "source_paused": True,
    "saved_expiring": False,  # TODO(F6.6)
}

_lock = threading.Lock()


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def notification_settings(path: Path) -> dict[str, bool]:
    """Every category with its current value: the stored choice, else the default. A missing or
    unreadable file, or a stored value that is not a boolean, reads as the default."""
    stored = _read(path).get("notifications")
    stored = stored if isinstance(stored, dict) else {}
    return {name: stored[name] if isinstance(stored.get(name), bool) else default
            for name, default in NOTIFICATION_DEFAULTS.items()}


def update_notification_settings(path: Path, changes: Mapping[str, Any]) -> dict[str, bool]:
    """Apply ``changes`` (category -> bool) and return every category. Raises ValueError for an
    unknown category or a non-boolean value; nothing is written then. Other settings in the file
    are kept."""
    unknown = sorted(set(changes) - set(NOTIFICATION_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown notification categories: {', '.join(unknown)}")
    bad = sorted(name for name, value in changes.items() if not isinstance(value, bool))
    if bad:
        raise ValueError(f"notification categories take true or false: {', '.join(bad)}")
    with _lock:
        data = _read(path)
        merged = {**notification_settings(path), **changes}
        data["notifications"] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return merged


# Unofficial sources (plan rule I1): off unless chosen. When no choice is stored, an install
# that already set the source up the old way (this environment variable) counts as opted in.
UNOFFICIAL_LEGACY_ENV: dict[str, str] = {"indeed": "INDEED_API_KEY"}


def unofficial_source_choice(path: Path | None, source: str) -> bool | None:
    """The stored choice for the unofficial ``source`` (``unofficial_sources.<source>``), or
    None when the user has not chosen (or ``path`` is None)."""
    stored = _read(path).get("unofficial_sources") if path is not None else None
    if isinstance(stored, dict) and isinstance(stored.get(source), bool):
        return bool(stored[source])
    return None


def unofficial_source_enabled(path: Path | None, source: str, *, used_before: bool = False) -> bool:
    """Whether the user chose to collect from the unofficial ``source``. The stored choice
    wins; else the legacy setup (``UNOFFICIAL_LEGACY_ENV``) or ``used_before`` (the caller
    knows the install already ran this source: an observed choice, plan rule I1); else off.
    ``path`` None (no settings file known, e.g. a test): only the legacy signals count."""
    choice = unofficial_source_choice(path, source)
    if choice is not None:
        return choice
    env = UNOFFICIAL_LEGACY_ENV.get(source)
    return used_before or bool(env and os.environ.get(env, "").strip())


BACKGROUND_DEFAULTS: dict[str, Any] = {"enabled": True, "daily_time": "08:00"}


def parse_daily_time(value: Any) -> tuple[int, int] | None:
    """``(hour, minute)`` for a "HH:MM" string in 00:00..23:59, else None."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def background_settings(path: Path | None) -> dict[str, Any]:
    """``{"enabled": bool, "daily_time": "HH:MM"}``: the stored values, else the defaults (a
    missing or unreadable file, or an invalid value, reads as the default)."""
    stored = _read(path).get("background") if path is not None else None
    stored = stored if isinstance(stored, dict) else {}
    enabled = stored.get("enabled")
    parsed = parse_daily_time(stored.get("daily_time"))
    return {
        "enabled": enabled if isinstance(enabled, bool) else BACKGROUND_DEFAULTS["enabled"],
        "daily_time": f"{parsed[0]:02d}:{parsed[1]:02d}" if parsed else BACKGROUND_DEFAULTS["daily_time"],
    }


def update_background_settings(path: Path, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Apply ``changes`` (``enabled``: bool, ``daily_time``: "HH:MM") and return the settings.
    Raises ValueError for an unknown key or an invalid value; nothing is written then. Other
    settings in the file are kept."""
    unknown = sorted(set(changes) - set(BACKGROUND_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown background settings: {', '.join(unknown)}")
    if "enabled" in changes and not isinstance(changes["enabled"], bool):
        raise ValueError("enabled takes true or false")
    if "daily_time" in changes:
        parsed = parse_daily_time(changes["daily_time"])
        if parsed is None:
            raise ValueError("daily_time takes a time of day as HH:MM, from 00:00 to 23:59")
        changes = {**changes, "daily_time": f"{parsed[0]:02d}:{parsed[1]:02d}"}
    with _lock:
        data = _read(path)
        merged = {**background_settings(path), **changes}
        data["background"] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return merged
