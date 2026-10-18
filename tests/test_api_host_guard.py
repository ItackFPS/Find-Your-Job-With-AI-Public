"""Issue #59: requests whose Host is not the local machine are rejected.

DNS rebinding: a page on http://attacker.example:8000 re-resolves its own
name to 127.0.0.1. The browser then talks to the local backend with
``Host: attacker.example:8000`` and ``Origin: http://attacker.example:8000``
— same-origin as far as the browser (and an Origin-only check) is concerned,
so it could both trigger writes and read responses. Checking Host closes
that: a rebinding page can never present a loopback Host.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from src.api.origin_guard import LocalHostGuardMiddleware, OriginGuardMiddleware

_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def _app(extra_hosts: tuple[str, ...] = ()) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    calls: list[str] = []

    @app.post("/api/scan")
    def scan() -> dict:
        calls.append("scan")
        return {"ok": True}

    @app.get("/api/jobs")
    def jobs() -> dict:
        calls.append("jobs")
        return {"jobs": []}

    @app.get("/api/events")
    def events() -> StreamingResponse:
        calls.append("events")
        return StreamingResponse(iter(["data: 1\n\n"]), media_type="text/event-stream")

    @app.get("/")
    def spa() -> dict:
        calls.append("spa")
        return {"ok": True}

    # Same order as src/api/app.py: the Host guard is added last, so it is
    # the outermost middleware and runs first.
    app.add_middleware(OriginGuardMiddleware, allowed_origins=_DEV_ORIGINS)
    app.add_middleware(LocalHostGuardMiddleware, extra_hosts=extra_hosts)
    return app, calls


def _client(host: str, extra_hosts: tuple[str, ...] = ()) -> tuple[TestClient, list[str]]:
    app, calls = _app(extra_hosts)
    return TestClient(app, base_url=f"http://{host}"), calls


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8000", "localhost:8000", "LOCALHOST:8000", "localhost", "[::1]:8000", "[::1]"],
)
def test_loopback_hosts_are_served(host: str) -> None:
    # Host set explicitly: Starlette's TestClient cannot parse an IPv6 base_url.
    client, calls = _client("127.0.0.1:8000")
    assert client.get("/api/jobs", headers={"Host": host}).status_code == 200
    # No Origin: curl / n8n / scripts.
    assert client.post("/api/scan", headers={"Host": host}).status_code == 200
    assert calls == ["jobs", "scan"]


def test_vite_dev_proxy_request_is_served() -> None:
    # frontend/vite.config.ts proxies /api with changeOrigin: true, so the
    # backend sees Host 127.0.0.1:8000 and the browser's dev-server Origin.
    client, calls = _client("127.0.0.1:8000")
    r = client.post("/api/scan", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert calls == ["scan"]


def test_same_origin_spa_write_is_served() -> None:
    client, _calls = _client("localhost:8000")
    r = client.post("/api/scan", headers={"Origin": "http://localhost:8000"})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/api/scan"])
def test_dns_rebinding_write_is_rejected_before_the_handler_runs(path: str) -> None:
    client, calls = _client("attacker.example:8000")
    r = client.post(path, headers={"Origin": "http://attacker.example:8000"})
    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid host header"}
    assert calls == []


@pytest.mark.parametrize("path", ["/api/jobs", "/api/events", "/"])
def test_dns_rebinding_reads_are_rejected(path: str) -> None:
    # CORS does not protect these: after rebinding the page is same-origin.
    client, calls = _client("attacker.example:8000")
    r = client.get(path, headers={"Origin": "http://attacker.example:8000"})
    assert r.status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    "host",
    [
        "localhost.attacker.example:8000",
        "127.0.0.1.nip.io:8000",
        "attacker.example",
        "[::1].attacker.example",
        "[::1]x:8000",
        "localhost:8000:1",
        "localhost:abc",
        "127.0.0.2:8000",
        "0.0.0.0:8000",
        "",
    ],
)
def test_lookalike_and_malformed_hosts_are_rejected(host: str) -> None:
    app, calls = _app()
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    r = client.get("/api/jobs", headers={"Host": host})
    assert r.status_code == 400, host
    assert calls == []


def test_loopback_host_does_not_bypass_the_origin_check() -> None:
    # A foreign page posting to http://127.0.0.1:8000 has a loopback Host but
    # a foreign Origin: still refused by the #12 Origin guard.
    client, calls = _client("127.0.0.1:8000")
    r = client.post("/api/scan", headers={"Origin": "https://attacker.example"})
    assert r.status_code == 403
    assert calls == []


def test_extra_hosts_are_opt_in() -> None:
    client, _ = _client("host.docker.internal:8000")
    assert client.get("/api/jobs").status_code == 400

    client, calls = _client("host.docker.internal:8000", extra_hosts=("Host.Docker.Internal",))
    assert client.get("/api/jobs").status_code == 200
    assert calls == ["jobs"]


def test_real_app_rejects_a_rebinding_host_before_touching_routes() -> None:
    from src.api.app import app

    # No `with`: the lifespan does not start; the guard answers first.
    client = TestClient(app, base_url="http://attacker.example:8000")
    assert client.get("/api/jobs").status_code == 400
    r = client.post("/api/pipeline/run", headers={"Origin": "http://attacker.example:8000"})
    assert r.status_code == 400


def test_real_app_host_guard_is_the_outermost_guard() -> None:
    from src.api.app import app

    classes = [m.cls for m in app.user_middleware]
    # app.user_middleware lists the outermost middleware first.
    assert classes.index(LocalHostGuardMiddleware) < classes.index(OriginGuardMiddleware)
