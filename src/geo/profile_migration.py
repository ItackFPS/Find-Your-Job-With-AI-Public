"""Convert a v1 location profile (ADR 0005 département priorities) to the distance model.

Update plan §C3 / §G: automatic, idempotent, with a backup before the first rewrite.
The v1 fields are kept untouched; this only ADDS
``geography.home``, ``max_distance_km``, ``strict_distance`` and ``remote_policy``:

* home: the town named in the top département's label when it resolves to a commune
  of that département, else the population-weighted centre of that département
  (``precision: department``). The top département is the first ``preferred`` one,
  else the first listed;
* max_distance_km: covers every listed (non-excluded) département: the farthest
  département centre by road estimate (great-circle × 1.25) plus ``COVER_MARGIN_KM``,
  at least ``MIN_RADIUS_KM``, rounded up to 10 km and capped at ``MAX_RADIUS_KM``;
* remote_policy: "Remote France" is accepted when the v1 list has a non-excluded remote
  entry, or has no remote entry and the work modes allow remote; EU/worldwide stay off
  (v1 never preferred them); hybrid follows the work modes;
* strict_distance: off (v1 never excluded on location preferences).

A profile that already has a home, or lists no département, is left as it is.
``migrate_profile_all`` then runs the profile v2 conversion (F5.1) on the result.
"""

from __future__ import annotations

import logging
import math
import shutil
from pathlib import Path
from typing import Any

import yaml

from src.config_files import PROFILE_WRITE_LOCK, write_yaml_atomic
from src.geo.places import COMMUNE, department_place, distance_km, resolve_place

logger = logging.getLogger(__name__)

ROAD_FACTOR = 1.25
COVER_MARGIN_KM = 40.0
MIN_RADIUS_KM = 50
MAX_RADIUS_KM = 150
PERSONAL_PROFILE = "profile.local.yaml"
BACKUP_NAME = "profile.v1-backup.local.yaml"   # matches the gitignored config/*.local.yaml
# Copy taken before the profile v2 conversion (F5.1), for a profile whose v1 backup
# already holds the pre-distance-model file.
V2_BACKUP_NAME = "profile.pre-v2-backup.local.yaml"


def _norm_modes(modes: list[Any] | None) -> set[str]:
    return {str(m).lower().replace("-", "_").replace(" ", "_") for m in (modes or [])}


def migrate_profile_data(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(migrated copy, notes)``; ``notes`` is empty when nothing changed."""
    raw_geo = data.get("geography") or {}
    if not isinstance(raw_geo, dict):
        return data, []          # malformed: left for the profile validation to report
    geo = dict(raw_geo)
    if geo.get("home"):
        return data, []
    prefs = [p for p in (geo.get("location_preferences") or []) if isinstance(p, dict)]
    live = [p for p in prefs if p.get("status", "preferred") != "excluded"]
    depts = [p for p in live if p.get("type") == "department" and department_place(str(p.get("value")))]
    if not depts:
        return data, []

    top = next((p for p in depts if p.get("status", "preferred") == "preferred"), depts[0])
    top_code = str(top["value"])
    centre = department_place(top_code)
    assert centre is not None and centre.lat is not None and centre.lon is not None
    notes: list[str] = []
    town = resolve_place(f"{top['label']} ({top_code})") if top.get("label") else None
    home: dict[str, Any]
    if (town is not None and town.precision == COMMUNE and town.department == top_code
            and town.lat is not None and town.lon is not None):
        origin = (town.lat, town.lon)
        home = {"name": town.name, "insee": town.insee, "lat": town.lat, "lon": town.lon,
                "department": top_code, "precision": "commune"}
        notes.append(f"home set to {town.name} (the label of your top département {top_code})")
    else:
        origin = (round(centre.lat, 5), round(centre.lon, 5))
        home = {"name": centre.name, "insee": None, "lat": origin[0], "lon": origin[1],
                "department": top_code, "precision": "department"}
        notes.append(f"home set to the centre of {centre.name} ({top_code}); pick your town to refine it")

    reach = 0.0
    for p in depts:
        c = department_place(str(p["value"]))
        if c is not None and c.lat is not None and c.lon is not None:
            reach = max(reach, distance_km(origin, (c.lat, c.lon)) * ROAD_FACTOR)
    radius = max(MIN_RADIUS_KM, int(math.ceil((reach + COVER_MARGIN_KM) / 10.0) * 10))
    if radius > MAX_RADIUS_KM:
        notes.append(f"maximum distance capped at {MAX_RADIUS_KM} km (covering every listed département "
                     f"needed {radius} km)")
        radius = MAX_RADIUS_KM
    else:
        notes.append(f"maximum distance {radius} km covers your listed départements")
    for p in depts:
        c = department_place(str(p["value"]))
        if c is not None and c.lat is not None and c.lon is not None:
            km = distance_km(origin, (c.lat, c.lon)) * ROAD_FACTOR
            if km > radius:
                notes.append(f"{p.get('status', 'preferred')} département {p['value']} is ~{km:.0f} km away, "
                             f"beyond {radius} km: its offers stay visible but rank lower")
    excluded = sorted({str(p.get("value")) for p in prefs
                       if p.get("type") == "department" and p.get("status") == "excluded"})
    if excluded:
        notes.append(f"excluded départements kept as exclusions: {', '.join(excluded)}")

    modes = _norm_modes(data.get("work_modes"))
    remote_prefs = [p for p in prefs if p.get("type") == "remote"]
    if remote_prefs:
        remote_france = any(p.get("status", "preferred") != "excluded" for p in remote_prefs)
    else:
        remote_france = not modes or "remote" in modes
    policy = {"remote_france": remote_france, "remote_eu": False, "remote_worldwide": False,
              "hybrid": not modes or "hybrid" in modes}
    notes.append(f"remote France {'accepted' if remote_france else 'not accepted'}; hybrid "
                 f"{'accepted' if policy['hybrid'] else 'not accepted'}")

    geo.update(home=home, max_distance_km=radius, strict_distance=False, remote_policy=policy)
    if excluded:
        geo["excluded_departments"] = excluded
    migrated = dict(data)
    migrated["geography"] = geo
    return migrated, notes


def migrate_profile_all(data: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Run the distance-model then the profile v2 conversion (src/models/profile_v2_migration.py).

    Returns ``(migrated copy, location notes, v2 notes)``; both lists are empty when nothing
    changed. The v2 step runs second so its review notes describe the converted location."""
    from src.models.profile_v2_migration import migrate_v2_data

    migrated, geo_notes = migrate_profile_data(data)
    migrated, v2_notes = migrate_v2_data(migrated)
    return migrated, geo_notes, v2_notes


def backup_profile(path: Path, location: bool, v2: bool) -> None:
    """Copy ``path`` before a conversion rewrites it; an existing backup is never overwritten.

    The first conversion fills the v1 backup; a later one (the v2 conversion of a file
    already on the distance model) fills the pre-v2 backup. A plain v1 file is copied once."""
    v1 = path.with_name(BACKUP_NAME)
    if location and not v1.exists():
        shutil.copy2(path, v1)
        return
    pre_v2 = path.with_name(V2_BACKUP_NAME)
    if (location or v2) and not pre_v2.exists():
        shutil.copy2(path, pre_v2)


def migrate_profile_file(path: str | Path) -> list[str]:
    """Migrate a profile YAML file in place. Before the first rewrite the original is copied
    next to it (``backup_profile``). Returns the notes
    (empty: nothing to do). Raises on unreadable/invalid data; callers decide to fail open."""
    from src.models.profile import JobSearchProfile

    p = Path(path)
    original = p.read_text(encoding="utf-8")
    data = yaml.safe_load(original) or {}
    if not isinstance(data, dict):
        return []
    migrated, geo_notes, v2_notes = migrate_profile_all(data)
    notes = geo_notes + v2_notes
    if not notes:
        return []
    JobSearchProfile(**migrated)  # never write a profile the app cannot load
    with PROFILE_WRITE_LOCK:
        # A save (PUT /api/profile) may have landed since the read: never overwrite it.
        if p.read_text(encoding="utf-8") != original:
            logger.info("Profile %s changed during migration; left for the next run", p.name)
            return []
        backup_profile(p, bool(geo_notes), bool(v2_notes))
        write_yaml_atomic(p, migrated)
    logger.info("Profile %s migrated (backups next to it): %s", p.name, "; ".join(notes))
    return notes


def migrate_personal_profile(path: str | Path) -> list[str]:
    """Migrate ``path`` in place only when it is the personal ``profile.local.yaml``: the tracked
    default and pinned fixture profiles are never rewritten. Fail-open (logs, returns [])."""
    p = Path(path)
    if p.name != PERSONAL_PROFILE or not p.is_file():
        return []
    try:
        return migrate_profile_file(p)
    except Exception as exc:  # noqa: BLE001 — a failed migration must never stop a run
        logger.warning("Profile migration skipped: %s", exc)
        return []
