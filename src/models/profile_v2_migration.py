"""Convert a v1 profile (keyword lists, ``bac+N``) to profile v2 (update plan §C3, F5.1).

Automatic and idempotent: a profile with ``profile_version >= 2`` is left as it is. Only
ADDS v2 fields; the v1 ``experience`` and ``max_education`` fields are kept untouched
because the matching still reads them until F5.2/F5.3:

* targets.levels: the v1 preferred levels, minus the excluded ones; with no preferred
  level, every level the exclusions leave; with neither, none (every level is shown);
* education: ``max_education`` becomes one held qualification at its French/EQF level
  (bac+3 → 6, see ``src.models.levels.bac_plus_to_level``);
* experiences stay empty (v1 never stored them): the review screen asks for them;
* review: pending, with notes on what changed and on the location settings in effect
  (the 150 km cap and the kept excluded départements).

The file-level steps (backup, write) live with the location migration in
``src/geo/profile_migration.py``, which runs both.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.models.levels import (
    CAREER_LEVELS,
    V1_EXCLUDED_TO_LEVEL,
    V1_PREFERRED_TO_LEVEL,
    bac_plus_to_level,
)
from src.geo.profile_migration import MAX_RADIUS_KM
from src.models.profile import PROFILE_SCHEMA_VERSION


def _key(label: Any) -> str:
    return str(label).strip().lower().replace("-", "_").replace(" ", "_")


def _target_levels(exp: dict[str, Any], notes: list[str]) -> list[str]:
    preferred = [_key(x) for x in (exp.get("preferred_levels") or [])]
    excluded = [_key(x) for x in (exp.get("excluded_seniority") or [])]
    ruled_out = {V1_EXCLUDED_TO_LEVEL[x] for x in excluded if x in V1_EXCLUDED_TO_LEVEL}
    unknown = sorted({x for x in preferred if x not in V1_PREFERRED_TO_LEVEL}
                     | {x for x in excluded if x not in V1_EXCLUDED_TO_LEVEL})
    if unknown:
        notes.append(f"level labels not converted: {', '.join(unknown)}")
    if preferred:
        wanted = {V1_PREFERRED_TO_LEVEL[x] for x in preferred if x in V1_PREFERRED_TO_LEVEL}
    elif excluded:
        wanted = set(CAREER_LEVELS)
    else:
        wanted = set()
    levels = [lvl for lvl in CAREER_LEVELS if lvl in wanted and lvl not in ruled_out]
    if levels:
        notes.append(f"target levels: {', '.join(levels)}")
    else:
        notes.append("no target level set: offers of every level are shown")
    return levels


def _education(data: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    raw = data.get("max_education")
    if not raw:
        return []
    from src.requirements.education import parse_education_level

    bac_plus = parse_education_level(str(raw))
    if bac_plus is None:
        notes.append(f"education \"{raw}\" not converted to a level: add your qualification")
        return []
    level = bac_plus_to_level(bac_plus)
    notes.append(f"education {raw} became level {level}; add the name of your qualification")
    return [{"id": "edu-1", "qualification": str(raw), "level": level, "field": None,
             "institution": None, "year": None, "in_progress": False, "foreign": False}]


def _location_notes(data: dict[str, Any]) -> list[str]:
    raw_geo = data.get("geography")
    geo: dict[str, Any] = raw_geo if isinstance(raw_geo, dict) else {}
    notes: list[str] = []
    home = geo.get("home") if isinstance(geo.get("home"), dict) else None
    radius = geo.get("max_distance_km")
    if home and radius is not None:
        notes.append(f"home {home.get('name')}, maximum distance {radius} km")
        if radius == MAX_RADIUS_KM:
            beyond = ("on-site offers farther away are excluded (strict distance)" if geo.get("strict_distance")
                      else "farther départements stay visible but rank lower")
            notes.append(f"maximum distance is the {MAX_RADIUS_KM} km cap: {beyond}")
    excluded = geo.get("excluded_departments") or []
    if excluded:
        notes.append(f"excluded départements kept as exclusions: {', '.join(map(str, excluded))}")
    return notes


def migrate_v2_data(data: dict[str, Any], *, now: datetime | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return ``(migrated copy, notes)``; ``notes`` is empty when nothing changed."""
    try:
        version = int(data.get("profile_version") or 1)
    except (TypeError, ValueError):
        return data, []          # malformed: left for the profile validation to report
    if version >= PROFILE_SCHEMA_VERSION:
        return data, []
    exp = data.get("experience") or {}
    if not isinstance(exp, dict):
        return data, []          # malformed: left for the profile validation to report
    notes: list[str] = []
    targets = dict(data.get("targets") or {}) if isinstance(data.get("targets"), dict) else {}
    # A v1 file can carry the v2 defaults (a v1 profile saved by this version): empty
    # levels there were never chosen, so they are derived like a missing field.
    if not targets.get("levels"):
        targets["levels"] = _target_levels(exp, notes)
    if exp.get("max_years") is not None:
        notes.append(f"offers asking more than {exp['max_years']} years of experience stay excluded")
    education = data.get("education") or _education(data, notes)
    if not data.get("experiences"):
        notes.append("add your experiences: your years of experience are computed from them")
    notes.extend(_location_notes(data))

    migrated = dict(data)
    migrated.update(
        profile_version=PROFILE_SCHEMA_VERSION,
        targets=targets,
        education=education,
        experiences=list(data.get("experiences") or []),
        review={"pending": True, "notes": notes,
                "migrated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")},
    )
    return migrated, notes
