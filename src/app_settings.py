"""Application settings that are not part of the job-search profile (update plan F2.4).

Stored in ``data/settings.json`` (never in ``config/profile.yaml``: the profile is what searches are
built from, and changing it creates a new strategy version). Today it holds the desktop
notification categories the user opted into; the tray app reads them through
``GET /api/settings/notifications`` before each Windows notification.

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
