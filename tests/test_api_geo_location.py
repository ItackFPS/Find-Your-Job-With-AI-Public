"""API: offline commune autocomplete and the profile's distance-model fields (F4.3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.geo.profile_migration import BACKUP_NAME

FIX = Path(__file__).resolve().parent / "fixtures" / "location_migration"


@pytest.fixture
def client():
    from src.api.app import app

    return TestClient(app)


def test_places_autocomplete_is_offline_and_ranked(client):
    r = client.get("/api/geo/places", params={"q": "toul"})
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert {"Toulouse", "Toulon"} <= set(names[:3])
    toulon = next(p for p in r.json() if p["name"] == "Toulon")
    assert toulon["insee"] == "83137" and toulon["department"] == "83" and toulon["department_name"] == "Var"


def test_places_by_postcode_and_short_queries(client):
    assert [p["name"] for p in client.get("/api/geo/places", params={"q": "83000"}).json()] == ["Toulon"]
    assert client.get("/api/geo/places", params={"q": "t"}).json() == []
    assert len(client.get("/api/geo/places", params={"q": "saint", "limit": 5}).json()) == 5
    assert client.get("/api/geo/places", params={"q": "x", "limit": 99}).status_code == 422


@pytest.fixture
def v1_profile(tmp_path, monkeypatch):
    import src.api.routes.profile as profile_route

    p = tmp_path / "profile.yaml"
    shutil.copy(FIX / "v1_departments_remote.yaml", p)
    monkeypatch.setattr(profile_route, "_PROFILE_PATH", p)
    return p


def test_get_profile_shows_the_migrated_view_without_writing(client, v1_profile):
    original = v1_profile.read_text(encoding="utf-8")
    geo = client.get("/api/profile").json()["geography"]
    assert geo["home"]["department"] == "83" and geo["max_distance_km"] == 150
    assert geo["remote_policy"]["remote_france"] is True and geo["strict_distance"] is False
    assert geo["excluded_departments"] == ["13"]
    assert len(geo["location_preferences"]) == 5                       # v1 list still served
    # a read never writes: no rewrite, no backup
    assert v1_profile.read_text(encoding="utf-8") == original
    assert not (v1_profile.parent / BACKUP_NAME).exists()


def test_first_save_over_a_v1_profile_keeps_a_backup(client, v1_profile):
    original = v1_profile.read_text(encoding="utf-8")
    view = client.get("/api/profile").json()
    assert client.put("/api/profile", json={"geography": view["geography"]}).status_code == 200
    assert (v1_profile.parent / BACKUP_NAME).read_text(encoding="utf-8") == original
    saved = yaml.safe_load(v1_profile.read_text(encoding="utf-8"))["geography"]
    assert saved["home"]["department"] == "83" and saved["excluded_departments"] == ["13"]
    assert not [f for f in v1_profile.parent.iterdir() if f.suffix == ".tmp"]
    # a second save never replaces the first backup
    assert client.put("/api/profile", json={"geography": {"max_distance_km": 80}}).status_code == 200
    assert (v1_profile.parent / BACKUP_NAME).read_text(encoding="utf-8") == original


def test_home_can_be_cleared(client, v1_profile):
    view = client.get("/api/profile").json()
    client.put("/api/profile", json={"geography": view["geography"]})
    assert client.put("/api/profile", json={"geography": {"home": None}}).status_code == 200
    raw = yaml.safe_load(v1_profile.read_text(encoding="utf-8"))
    assert raw["geography"]["home"] is None


def test_put_profile_round_trips_home_radius_and_policy(client, v1_profile):
    home = {"name": "Toulon", "insee": "83137", "lat": 43.1361, "lon": 5.9334, "department": "83",
            "precision": "commune"}
    r = client.put("/api/profile", json={"geography": {
        "home": home, "max_distance_km": 40, "strict_distance": True,
        "remote_policy": {"remote_france": True, "remote_eu": True, "remote_worldwide": False, "hybrid": False}}})
    assert r.status_code == 200, r.text
    geo = client.get("/api/profile").json()["geography"]
    assert geo["home"] == home and geo["max_distance_km"] == 40 and geo["strict_distance"] is True
    assert geo["remote_policy"] == {"remote_france": True, "remote_eu": True, "remote_worldwide": False,
                                    "hybrid": False}
    assert len(geo["location_preferences"]) == 5                       # merge kept the v1 list


@pytest.mark.parametrize("km", [0, 4, 301, 1000])
def test_put_profile_rejects_a_radius_outside_the_slider_range(client, v1_profile, km):
    r = client.put("/api/profile", json={"geography": {"max_distance_km": km}})
    assert r.status_code == 422
