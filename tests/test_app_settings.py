"""Notification categories in the app settings file (src/app_settings.py; update plan F2.4)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.app_settings import NOTIFICATION_DEFAULTS, notification_settings, update_notification_settings


def test_defaults_are_on_for_strong_matches_and_failures_only(tmp_path):
    assert notification_settings(tmp_path / "settings.json") == {
        "strong_matches": True, "search_failed": True, "source_paused": False, "saved_expiring": False}


def test_updates_are_stored_outside_the_profile_and_keep_other_settings(tmp_path):
    path = tmp_path / "data" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    result = update_notification_settings(path, {"source_paused": True, "strong_matches": False})
    assert result == {**NOTIFICATION_DEFAULTS, "source_paused": True, "strong_matches": False}
    assert notification_settings(path) == result
    assert json.loads(path.read_text(encoding="utf-8"))["other"] == 1


@pytest.mark.parametrize("changes", [{"nope": True}, {"strong_matches": "yes"}])
def test_bad_updates_are_refused_and_change_nothing(tmp_path, changes):
    path = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        update_notification_settings(path, changes)
    assert not path.exists()


def test_a_corrupt_file_reads_as_the_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert notification_settings(path) == NOTIFICATION_DEFAULTS
    path.write_text(json.dumps({"notifications": {"strong_matches": "x"}}), encoding="utf-8")
    assert notification_settings(path)["strong_matches"] is True


def test_settings_api_reads_and_updates_the_categories(tmp_path, monkeypatch):
    from src.api.app import app

    monkeypatch.setitem(app.state._state, "settings_path", str(tmp_path / "settings.json"))
    client = TestClient(app)
    body = client.get("/api/settings/notifications").json()
    assert body["categories"] == NOTIFICATION_DEFAULTS
    assert body["available"]["saved_expiring"] is False            # TODO(F6.6): no expiry data yet
    put = client.put("/api/settings/notifications", json={"categories": {"source_paused": True}})
    assert put.status_code == 200 and put.json()["categories"]["source_paused"] is True
    assert client.get("/api/settings/notifications").json()["categories"]["source_paused"] is True
    bad = client.put("/api/settings/notifications", json={"categories": {"weekly_digest": True}})
    assert bad.status_code == 422
