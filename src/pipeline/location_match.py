"""Shared resolver: which configured location preference does a job match?

Single source of truth for "does this job belong to one of the user's
configured departments/remote preference, and at what priority" — scoring
and Discord notification routing both call this instead of re-implementing
department/remote matching themselves. See profile.geography.location_preferences
(src/models/profile.py) for the preference list this resolves against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LocationMatch:
    type: str  # "department" | "remote"
    value: str
    status: str  # "preferred" | "acceptable" (an "excluded" match never produces a LocationMatch)
    priority: int  # index of the matched preference in the original location_preferences list
    slug: str  # stable id for Discord channel mapping, e.g. "dept-83", "remote-remote_france"
    label: str | None = None  # optional display label carried through from the matched preference


def slug_for(pref_type: str, value: str) -> str:
    """Stable Discord-channel-mapping id for a preference type/value pair,
    e.g. ("department", "83") -> "dept-83". Public so other modules (e.g.
    src.notifications.discord_reconciliation) can derive the same slugs
    for preferences that don't come from a job match, without duplicating
    this logic.
    """
    if pref_type == "department":
        return f"dept-{value}"
    return f"remote-{value}"


def _matches(
    pref: dict[str, Any],
    job_department: str | None,
    job_remote: bool,
    job_countries: list[str] | None,
) -> bool:
    pref_type = pref.get("type")
    if pref_type == "department":
        return job_department is not None and str(job_department) == str(pref.get("value"))
    if pref_type == "remote":
        if not job_remote:
            return False
        # "Remote France"-style preferences require a French signal — an
        # explicit France entry in job_countries, or no country data at all
        # (many remote listings omit countries; a resolved department would
        # already have made this a department match instead). A distinct
        # remote-anywhere preference is not modeled — out of the current
        # scope per the approved plan.
        countries_lower = {c.lower() for c in (job_countries or [])}
        return (not countries_lower) or ("france" in countries_lower)
    return False


def resolve_job_location(
    job_department: str | None,
    job_remote: bool,
    job_countries: list[str] | None,
    location_preferences: list[dict[str, Any]],
) -> LocationMatch | None:
    """Which configured preference does this job match, and at what priority?

    Resolution:
    1. Find every preference (in list order) the job matches.
    2. Drop ``excluded`` matches from consideration — exclusion vetoes only
       that one preference, never the whole job. If a ``preferred``/
       ``acceptable`` match exists anywhere else in the list, it still wins,
       regardless of its position relative to the excluded entry.
    3. Return the surviving match with the lowest list index (highest
       priority), or ``None`` when the job matches nothing, or matches only
       excluded preferences.

    This function decides matching ONLY. It has no opinion on score values,
    Discord destinations, or notification eligibility — those are decided by
    each caller from the returned ``LocationMatch``.
    """
    candidates: list[tuple[int, dict[str, Any]]] = []
    for idx, pref in enumerate(location_preferences or []):
        if _matches(pref, job_department, job_remote, job_countries):
            candidates.append((idx, pref))

    non_excluded = [(idx, pref) for idx, pref in candidates if pref.get("status") != "excluded"]
    if not non_excluded:
        return None

    idx, pref = min(non_excluded, key=lambda pair: pair[0])
    value = str(pref["value"])
    return LocationMatch(
        type=pref["type"],
        value=value,
        status=pref.get("status", "preferred"),
        priority=idx,
        slug=slug_for(pref["type"], value),
        label=pref.get("label") or None,
    )
