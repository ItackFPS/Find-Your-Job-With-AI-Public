"""v1 profile -> profile v2 (src/models/profile_v2_migration.py, F5.1), on 3 fixture profiles."""

from __future__ import annotations

import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.geo.profile_migration import BACKUP_NAME, V2_BACKUP_NAME, migrate_profile_all, migrate_profile_file
from src.models.levels import bac_plus_to_level
from src.models.profile import (
    PROFILE_SCHEMA_VERSION,
    ExperienceEntry,
    JobSearchProfile,
    compute_experience_years,
)
from src.models.profile_v2_migration import migrate_v2_data

FIX = Path(__file__).resolve().parent / "fixtures" / "profile_v2_migration"
ALL = ["v1_junior_bac3.yaml", "v1_exclusions_only_master.yaml", "v1_empty_experience.yaml"]
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def test_junior_profile_becomes_targets_and_a_level_6():
    data = _load("v1_junior_bac3.yaml")
    migrated, notes = migrate_v2_data(data, now=NOW)
    assert migrated["profile_version"] == PROFILE_SCHEMA_VERSION
    assert migrated["targets"]["levels"] == ["graduate", "junior"]
    assert migrated["education"] == [{"id": "edu-1", "qualification": "bac+3", "level": 6, "field": None,
                                      "institution": None, "year": None, "in_progress": False, "foreign": False}]
    assert migrated["experiences"] == []
    assert migrated["review"] == {"pending": True, "notes": notes, "migrated_at": "2026-10-25T00:00:00+00:00"}
    assert "offers asking more than 3 years of experience stay excluded" in notes
    # v1 fields the matching still reads are kept exactly
    assert migrated["experience"] == data["experience"] and migrated["max_education"] == "bac+3"
    profile = JobSearchProfile(**migrated)
    assert profile.highest_education_level() == 6 and profile.experience_years() is None


def test_exclusions_only_profile_keeps_every_other_level():
    migrated, notes = migrate_v2_data(_load("v1_exclusions_only_master.yaml"), now=NOW)
    assert migrated["targets"]["levels"] == ["internship", "alternance", "graduate", "junior", "mid", "lead", "manager"]
    assert migrated["education"][0]["level"] == 7
    assert any("Master became level 7" in n for n in notes)


def test_empty_profile_has_no_target_and_reports_unknown_labels():
    migrated, notes = migrate_v2_data(_load("v1_empty_experience.yaml"), now=NOW)
    assert migrated["targets"]["levels"] == [] and migrated["education"] == []
    assert "level labels not converted: wizard" in notes
    assert "no target level set: offers of every level are shown" in notes


@pytest.mark.parametrize("name", ALL)
def test_migration_is_idempotent_and_round_trips(name):
    migrated, _geo, v2_notes = migrate_profile_all(_load(name))
    assert v2_notes
    dumped = JobSearchProfile(**migrated).model_dump()
    assert JobSearchProfile(**dumped).model_dump() == dumped
    again, geo_again, v2_again = migrate_profile_all(dumped)
    assert (geo_again, v2_again) == ([], []) and again == dumped


def test_review_notes_list_the_location_cap_and_kept_exclusions():
    _migrated, geo_notes, v2_notes = migrate_profile_all(_load("v1_junior_bac3.yaml"))
    assert geo_notes
    assert "excluded départements kept as exclusions: 13" in v2_notes
    assert any(n.startswith("home Lyon, maximum distance") for n in v2_notes)
    capped = {"geography": {"home": {"name": "Brest", "lat": 48.39, "lon": -4.49}, "max_distance_km": 150}}
    assert any("150 km cap" in n for n in migrate_v2_data(capped)[1])


def test_file_migration_backs_up_before_v2_even_after_a_location_migration(tmp_path):
    p = tmp_path / "profile.local.yaml"
    shutil.copy(FIX / "v1_junior_bac3.yaml", p)
    (tmp_path / BACKUP_NAME).write_text("pre-distance backup", encoding="utf-8")
    original = p.read_text(encoding="utf-8")
    assert migrate_profile_file(p)
    assert (tmp_path / BACKUP_NAME).read_text(encoding="utf-8") == "pre-distance backup"
    assert (tmp_path / V2_BACKUP_NAME).read_text(encoding="utf-8") == original
    first = p.read_text(encoding="utf-8")
    assert JobSearchProfile(**yaml.safe_load(first)).review.pending
    assert migrate_profile_file(p) == [] and p.read_text(encoding="utf-8") == first


@pytest.mark.parametrize("bac_plus,level", [(0, 4), (1, 4), (2, 5), (3, 6), (4, 6), (5, 7), (8, 8)])
def test_bac_plus_maps_to_french_eqf_levels(bac_plus, level):
    assert bac_plus_to_level(bac_plus) == level


def test_experience_years_merge_overlaps_and_honour_the_override():
    entries = [
        ExperienceEntry(id="a", role="Dev", start="2023-01", end="2023-12"),
        ExperienceEntry(id="b", role="Dev", start="2023-07", end="2024-06"),   # overlaps a
        ExperienceEntry(id="c", role="Waiter", start="2020-01", end="2020-12", relevant=False),
        ExperienceEntry(id="d", role="Dev", start="2026-01"),                   # ongoing
    ]
    assert compute_experience_years(entries, date(2026, 6, 15)) == 2.0
    profile = JobSearchProfile(experiences=entries)
    assert profile.experience_years(date(2026, 6, 15)) == 2.0
    assert JobSearchProfile(experiences=entries, targets={"years_override": 4}).experience_years() == 4


def test_invalid_v2_values_are_rejected():
    with pytest.raises(ValueError):
        JobSearchProfile(targets={"levels": ["wizard"]})
    with pytest.raises(ValueError):
        JobSearchProfile(education=[{"id": "e", "qualification": "Bac+2", "level": 2}])
    with pytest.raises(ValueError):
        JobSearchProfile(experiences=[{"id": "x", "role": "Dev", "start": "2024-13"}])


@pytest.fixture
def api_v1_profile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import src.api.routes.profile as profile_route
    from src.api.app import app

    p = tmp_path / "profile.local.yaml"
    shutil.copy(FIX / "v1_junior_bac3.yaml", p)
    monkeypatch.setattr(profile_route, "_PROFILE_PATH", p)
    return TestClient(app), p


def test_api_shows_the_pending_review_and_saving_dismisses_it(api_v1_profile):
    client, p = api_v1_profile
    original = p.read_text(encoding="utf-8")
    body = client.get("/api/profile").json()
    assert body["profile_version"] == 2 and body["review"]["pending"] is True
    assert body["targets"]["levels"] == ["graduate", "junior"] and body["education"][0]["level"] == 6
    assert p.read_text(encoding="utf-8") == original              # a read never writes
    body["review"]["pending"] = False
    saved = client.put("/api/profile", json=body)
    assert saved.status_code == 200 and saved.json()["review"]["pending"] is False
    assert (p.parent / V2_BACKUP_NAME).read_text(encoding="utf-8") == original
    after = client.get("/api/profile").json()
    assert after["review"]["pending"] is False and after["targets"]["levels"] == ["graduate", "junior"]
