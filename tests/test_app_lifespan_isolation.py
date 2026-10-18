"""Lifespan tests must never reach the real config/ (issue #46).

The leak needed three things at once: a Discord webhook env var left behind by
an earlier test, the app's system-status worker running with a
NotificationEngine bound to the real config/, and `httpx` patched globally by
another test so the worker "created" a message whose id it then persisted to
config/notifications.local.yaml.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from tests.conftest import _REAL_CONFIG_DIR


def test_system_status_worker_never_persists_to_the_real_config(isolated_app, monkeypatch):
    import src.notifications.engine as engine_module
    from src.notifications.system_status_worker import SystemStatusWorker

    monkeypatch.setenv("DISCORD_SYSTEM_STATUS_WEBHOOK_URL", "https://discord.com/api/webhooks/1/tok")
    writes: list[str] = []
    # Records instead of writing, so a regression can never touch the real file.
    monkeypatch.setattr(
        engine_module, "save_discord_channel_registries",
        lambda config_dir, **_kw: writes.append(str(config_dir)),
    )
    ticked = threading.Event()
    original_tick = SystemStatusWorker.tick

    def tick(self):
        original_tick(self)
        ticked.set()

    monkeypatch.setattr(SystemStatusWorker, "tick", tick)
    unknown_webhook = MagicMock(spec=httpx.Response, status_code=404)
    created = MagicMock(spec=httpx.Response, status_code=200)
    created.json.return_value = {"id": "new-message-id"}

    with patch("httpx.patch", return_value=unknown_webhook), patch("httpx.post", return_value=created):
        with TestClient(isolated_app):
            assert ticked.wait(timeout=10), "system-status worker never ticked"

    real = str(_REAL_CONFIG_DIR.resolve())
    assert all(str(Path(d).resolve()) != real for d in writes), writes
