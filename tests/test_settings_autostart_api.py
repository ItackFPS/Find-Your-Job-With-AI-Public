"""M3: Start with Windows from Settings, with no terminal and no tray menu.

The registry is never touched: the Autostart registry functions are replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.launcher import autostart as module


@pytest.fixture
def registry(monkeypatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(module, "_read_value", lambda: store.get("value"))
    monkeypatch.setattr(module, "_write_value", lambda v: store.__setitem__("value", v))
    monkeypatch.setattr(module, "_delete_value", lambda: store.pop("value", None))
    monkeypatch.setattr(module.sys, "platform", "win32")
    return store


def _install(tmp_path: Path, isolated_app, monkeypatch, runtime: bool) -> TestClient:
    (tmp_path / "config").mkdir()
    if runtime:
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "pythonw.exe").write_bytes(b"")
    monkeypatch.setitem(isolated_app.state._state, "config_dir", str(tmp_path / "config"))
    return TestClient(isolated_app)


def test_off_by_default_and_reports_support(tmp_path, isolated_app, monkeypatch, registry):
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=True)
    assert client.get("/api/settings/autostart").json() == {"supported": True, "enabled": False}


def test_turning_on_writes_the_same_command_as_the_tray(tmp_path, isolated_app, monkeypatch, registry):
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=True)
    assert client.put("/api/settings/autostart", json={"enabled": True}).json()["enabled"] is True
    assert registry["value"] == module.command(tmp_path)
    assert "tray --window none" in registry["value"]


def test_turning_off_removes_the_value(tmp_path, isolated_app, monkeypatch, registry):
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=True)
    client.put("/api/settings/autostart", json={"enabled": True})
    assert client.put("/api/settings/autostart", json={"enabled": False}).json()["enabled"] is False
    assert "value" not in registry


def test_another_installations_value_is_left_alone(tmp_path, isolated_app, monkeypatch, registry):
    registry["value"] = "another copy"
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=True)
    assert client.get("/api/settings/autostart").json()["enabled"] is False
    client.put("/api/settings/autostart", json={"enabled": False})
    assert registry["value"] == "another copy"


def test_a_source_checkout_cannot_start_at_sign_in(tmp_path, isolated_app, monkeypatch, registry):
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=False)
    assert client.get("/api/settings/autostart").json() == {"supported": False, "enabled": False}
    response = client.put("/api/settings/autostart", json={"enabled": True})
    assert response.status_code == 409
    assert "value" not in registry


@pytest.mark.parametrize("body", [{"enabled": "yes"}, {"enabled": 1}, {}])
def test_only_a_boolean_is_accepted(tmp_path, isolated_app, monkeypatch, registry, body):
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=True)
    assert client.put("/api/settings/autostart", json=body).status_code == 422
    assert "value" not in registry


def test_a_registry_error_is_reported_not_raised(tmp_path, isolated_app, monkeypatch, registry):
    def denied(_value):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(module, "_write_value", denied)
    client = _install(tmp_path, isolated_app, monkeypatch, runtime=True)
    response = client.put("/api/settings/autostart", json={"enabled": True})
    assert response.status_code == 500 and response.json()["detail"] == "autostart_failed"
