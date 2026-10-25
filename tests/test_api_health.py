"""GET /api/health: how the launcher recognises this backend."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.api import app as app_module
from src.api import peer_guard
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


# -- the launcher's instance token (update plan §C7, §E4) ------------------------------------------


def _scope(client: tuple[str, int], headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {"type": "http", "method": "GET", "path": "/api/health", "headers": headers or [],
            "client": client, "server": ("127.0.0.1", 8123), "query_string": b""}


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(app_module, "_INSTANCE_TOKEN", "the-instance-token")
    monkeypatch.setattr(peer_guard, "current_owner", lambda: "S-1-5-21-me")
    return "the-instance-token"


def test_health_returns_the_token_to_a_loopback_process_of_the_same_account(token, monkeypatch):
    monkeypatch.setattr(peer_guard, "connection_owner", lambda client, server: "S-1-5-21-me")
    body = app_module.health(Request(_scope(("127.0.0.1", 50001))))
    assert body["instance_token"] == token


@pytest.mark.parametrize("client, owner, headers", [
    (("127.0.0.1", 50001), "S-1-5-21-someone-else", []),            # another OS account (e.g. the lab's)
    (("127.0.0.1", 50001), None, []),                                # owner cannot be identified: fail closed
    (("192.168.1.20", 50001), "S-1-5-21-me", []),                    # not loopback
    (("127.0.0.1", 50001), "S-1-5-21-me", [(b"x-forwarded-for", b"127.0.0.1")]),  # address may be forged
])
def test_health_never_gives_the_token_to_another_peer(token, monkeypatch, client, owner, headers):
    monkeypatch.setattr(peer_guard, "connection_owner", lambda c, s: owner)
    body = app_module.health(Request(_scope(client, headers)))
    assert "instance_token" not in body and body["app"] == core.APP_ID


def test_health_through_http_gives_no_token_to_a_non_loopback_client(token):
    resp = TestClient(app).get("/api/health")          # the test client's peer is "testclient", not loopback
    assert resp.status_code == 200 and "instance_token" not in resp.json()


def test_the_token_is_taken_out_of_the_backends_environment():
    # what the backend starts (a lab sandbox, a collector's browser) must not inherit it
    import subprocess
    import sys

    code = ("import os; os.environ['FYJ_INSTANCE_TOKEN'] = 'tok'; import src.api.app as a; "
            "print(a._INSTANCE_TOKEN == 'tok', 'FYJ_INSTANCE_TOKEN' in os.environ)")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                         cwd=str(core.project_root()))
    assert run.stdout.split()[-2:] == ["True", "False"], run.stderr
