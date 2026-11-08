"""M3: the first-run setup is resumable, and an upgraded install is not sent through it."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config_files import local_config_path


@pytest.fixture
def client(tmp_path: Path, isolated_app, monkeypatch) -> TestClient:
    (tmp_path / "config").mkdir()
    monkeypatch.setitem(isolated_app.state._state, "config_dir", str(tmp_path / "config"))
    monkeypatch.setitem(isolated_app.state._state, "settings_path", str(tmp_path / "settings.json"))
    return TestClient(isolated_app)


def _saved_profile(tmp_path: Path) -> None:
    local_config_path(tmp_path / "config", "profile.yaml").write_text("target_domains: []\n", encoding="utf-8")


def test_a_fresh_install_needs_the_setup(client):
    assert client.get("/api/settings/onboarding").json() == {
        "started": False, "step": 0, "completed": False, "profile_saved": False}


def test_an_upgraded_install_with_a_profile_does_not(client, tmp_path):
    _saved_profile(tmp_path)
    assert client.get("/api/settings/onboarding").json()["completed"] is True


def test_the_step_survives_a_restart(client):
    assert client.put("/api/settings/onboarding", json={"step": 3}).json()["step"] == 3
    assert client.get("/api/settings/onboarding").json() == {
        "started": True, "step": 3, "completed": False, "profile_saved": False}


def test_saving_the_profile_during_the_setup_does_not_end_it(client, tmp_path):
    client.put("/api/settings/onboarding", json={"step": 1})
    _saved_profile(tmp_path)
    assert client.get("/api/settings/onboarding").json()["completed"] is False


def test_it_can_be_completed(client):
    client.put("/api/settings/onboarding", json={"step": 5})
    assert client.put("/api/settings/onboarding", json={"completed": True}).json() == {
        "started": True, "step": 5, "completed": True, "profile_saved": False}


@pytest.mark.parametrize("body", [{"step": -1}, {"step": 6}, {"step": True}, {"step": "2"},
                                  {"completed": "yes"}, {"completed": 1}])
def test_invalid_values_are_refused(client, body):
    assert client.put("/api/settings/onboarding", json=body).status_code == 422
    assert client.get("/api/settings/onboarding").json()["started"] is False


def test_other_settings_are_kept(client, tmp_path):
    client.put("/api/settings/background", json={"daily_time": "09:30"})
    client.put("/api/settings/onboarding", json={"step": 2})
    assert client.get("/api/settings/background").json()["daily_time"] == "09:30"
