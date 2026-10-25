"""v1 département priorities -> distance model (src/geo/profile_migration.py), on 3 fixture profiles."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from src.geo.profile_migration import (
    BACKUP_NAME,
    MAX_RADIUS_KM,
    migrate_personal_profile,
    migrate_profile_data,
    migrate_profile_file,
)
from src.models.profile import JobSearchProfile

FIX = Path(__file__).resolve().parent / "fixtures" / "location_migration"
ALL = ["v1_departments_remote.yaml", "v1_town_label_onsite.yaml", "v1_remote_only.yaml"]


def _load(name: str) -> dict:
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def test_departments_and_remote_profile():
    migrated, notes = migrate_profile_data(_load("v1_departments_remote.yaml"))
    geo = migrated["geography"]
    # top preferred département (83, first listed) -> its population-weighted centre
    assert geo["home"]["department"] == "83" and geo["home"]["precision"] == "department"
    assert geo["home"]["insee"] is None
    # 69 is far from the Var: the radius is capped, and the note says so
    assert geo["max_distance_km"] == MAX_RADIUS_KM and any("capped" in n for n in notes)
    assert geo["strict_distance"] is False
    assert geo["remote_policy"] == {"remote_france": True, "remote_eu": False,
                                    "remote_worldwide": False, "hybrid": True}
    # v1 fields are kept untouched (F5.1 removes them)
    assert geo["location_preferences"] == _load("v1_departments_remote.yaml")["geography"]["location_preferences"]
    # the explicit v1 exclusion is honoured, and every preference that changes meaning is noted
    assert geo["excluded_departments"] == ["13"]
    assert any("excluded départements kept" in n and "13" in n for n in notes)
    assert any(n.startswith("acceptable département 69") and "beyond 150 km" in n for n in notes)
    assert not any("département 04" in n for n in notes)      # 04 lies within the radius


def test_excluded_departments_still_count_after_migration():
    from src.pipeline.location_distance import assess, distance_config
    from src.models.job import Job

    profile = JobSearchProfile(**migrate_profile_data(_load("v1_departments_remote.yaml"))[0])
    cfg = distance_config(profile)
    assert cfg is not None
    marseille = assess(Job(id="m", title="Dev", location="13 - Marseille"), cfg)
    toulon = assess(Job(id="t", title="Dev", location="83 - Toulon"), cfg)
    assert marseille.score <= 0.5 < toulon.score


def test_town_label_becomes_a_commune_home_and_radius_covers_the_list():
    migrated, _ = migrate_profile_data(_load("v1_town_label_onsite.yaml"))
    geo = migrated["geography"]
    # the preferred entry wins over an earlier "acceptable" one; its label resolves to the town
    assert geo["home"]["name"] == "Nantes" and geo["home"]["insee"] == "44109"
    assert geo["home"]["precision"] == "commune"
    assert 50 <= geo["max_distance_km"] < MAX_RADIUS_KM and geo["max_distance_km"] % 10 == 0
    # on-site only, no remote entry: remote France and hybrid are not accepted
    assert geo["remote_policy"]["remote_france"] is False and geo["remote_policy"]["hybrid"] is False


def test_remote_only_profile_is_left_unchanged():
    data = _load("v1_remote_only.yaml")
    migrated, notes = migrate_profile_data(data)
    assert notes == [] and migrated == data


@pytest.mark.parametrize("name", ALL)
def test_round_trip_and_idempotent(name):
    migrated, _ = migrate_profile_data(_load(name))
    profile = JobSearchProfile(**migrated)                      # the result is a valid profile
    dumped = profile.model_dump()
    assert JobSearchProfile(**dumped) == profile                 # and survives a save/load round trip
    again, notes = migrate_profile_data(dumped if profile.geography.home else migrated)
    assert notes == []                                           # a second pass changes nothing
    assert again == (dumped if profile.geography.home else migrated)


def test_file_migration_writes_a_backup_once(tmp_path):
    p = tmp_path / "profile.local.yaml"
    shutil.copy(FIX / "v1_departments_remote.yaml", p)
    original = p.read_text(encoding="utf-8")
    assert migrate_profile_file(p)
    backup = tmp_path / BACKUP_NAME
    assert backup.read_text(encoding="utf-8") == original
    first = p.read_text(encoding="utf-8")
    assert "home:" in first and "location_preferences:" in first
    # idempotent: nothing to do, the file and the backup stay as they are
    assert migrate_profile_file(p) == []
    assert p.read_text(encoding="utf-8") == first and backup.read_text(encoding="utf-8") == original
    # a later migration never overwrites the first backup
    p.write_text(original, encoding="utf-8")
    backup.write_text("first backup", encoding="utf-8")
    assert migrate_profile_file(p)
    assert backup.read_text(encoding="utf-8") == "first backup"


def test_migration_never_overwrites_a_save_made_meanwhile(tmp_path, monkeypatch):
    import src.geo.profile_migration as pm

    p = tmp_path / "profile.local.yaml"
    shutil.copy(FIX / "v1_departments_remote.yaml", p)
    real = pm.migrate_profile_data

    def racing(data):
        out = real(data)
        p.write_text("target_domains:\n- data\n", encoding="utf-8")    # a PUT lands meanwhile
        return out

    monkeypatch.setattr(pm, "migrate_profile_data", racing)
    assert pm.migrate_profile_file(p) == []
    assert p.read_text(encoding="utf-8") == "target_domains:\n- data\n"
    assert not (tmp_path / BACKUP_NAME).exists()
    assert not [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]


def test_only_the_personal_profile_is_rewritten(tmp_path):
    tracked = tmp_path / "profile.yaml"
    shutil.copy(FIX / "v1_departments_remote.yaml", tracked)
    before = tracked.read_text(encoding="utf-8")
    assert migrate_personal_profile(tracked) == []
    assert tracked.read_text(encoding="utf-8") == before and not (tmp_path / BACKUP_NAME).exists()
    assert migrate_personal_profile(tmp_path / "profile.local.yaml") == []   # missing: no-op


def test_invalid_personal_profile_fails_open(tmp_path):
    p = tmp_path / "profile.local.yaml"
    p.write_text("geography: [not, a, mapping]\n", encoding="utf-8")
    assert migrate_personal_profile(p) == []
