"""GET /api/health: how the launcher recognises this backend."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api.app import app
from src.launcher import core


def test_health_identifies_the_app():
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": core.APP_ID, "version": app.version,
                           "pid": os.getpid()}


def test_launcher_and_backend_agree_on_the_app_id():
    assert app_module.APP_ID == core.APP_ID
    assert app.title == core.APP_TITLE
