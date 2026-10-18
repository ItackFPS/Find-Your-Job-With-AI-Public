"""Issue #12: state-changing API requests from other web origins are rejected.

A body-less POST is a CORS "simple request": a page on another site can make
the browser send it to the local backend (CORS only hides the response). The
guard rejects such requests by Origin while leaving same-origin UI calls, the
Vite dev server, and Origin-less clients (curl, n8n) untouched.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from src.api.origin_guard import OriginGuardMiddleware

_ALLOWED = ["http://localhost:5173", "http://127.0.0.1:5173"]


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    calls: list[str] = []

    @app.post("/api/scan")
    def scan() -> dict:
        calls.append("scan")
        return {"ok": True}

    @app.put("/api/profile")
    def put_profile(payload: dict) -> dict:
        calls.append("profile")
        return payload

    @app.get("/api/status")
    def status() -> dict:
        return {"ok": True}

    @app.get("/api/events")
    def events() -> StreamingResponse:
        return StreamingResponse(iter(["data: 1\n\n"]), media_type="text/event-stream")

    @app.post("/not-api/hook")
    def hook() -> dict:
        calls.append("hook")
        return {"ok": True}

    app.add_middleware(OriginGuardMiddleware, allowed_origins=_ALLOWED)
    test_client = TestClient(app, base_url="http://127.0.0.1:8000")
    test_client.calls = calls  # type: ignore[attr-defined]
    return test_client


def test_post_without_origin_is_allowed(client: TestClient) -> None:
    assert client.post("/api/scan").status_code == 200
    assert client.calls == ["scan"]


def test_same_origin_post_is_allowed(client: TestClient) -> None:
    r = client.post("/api/scan", headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


@pytest.mark.parametrize("origin", _ALLOWED)
def test_allow_listed_dev_origin_is_allowed(client: TestClient, origin: str) -> None:
    assert client.post("/api/scan", headers={"Origin": origin}).status_code == 200


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "null", "http://localhost:8000.evil.example", "http://127.0.0.1:9999"],
)
def test_foreign_origin_post_is_rejected_before_the_handler_runs(
    client: TestClient, origin: str
) -> None:
    r = client.post("/api/scan", headers={"Origin": origin})
    assert r.status_code == 403
    assert r.json() == {"detail": "Cross-origin request rejected"}
    assert client.calls == []


def test_foreign_origin_json_put_is_rejected(client: TestClient) -> None:
    r = client.put("/api/profile", json={"a": 1}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.calls == []


def test_reads_are_never_blocked(client: TestClient) -> None:
    headers = {"Origin": "https://evil.example"}
    assert client.get("/api/status", headers=headers).status_code == 200
    r = client.get("/api/events", headers=headers)
    assert r.status_code == 200
    assert r.text == "data: 1\n\n"


def test_non_api_paths_are_not_guarded(client: TestClient) -> None:
    r = client.post("/not-api/hook", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_real_app_blocks_foreign_origin_scan_trigger() -> None:
    from src.api.app import app

    # No `with`: the app lifespan (scheduler, workers, real data/ DB) must not
    # start — the guard answers before any route or app.state is touched.
    real = TestClient(app, base_url="http://127.0.0.1:8000")
    for path in ("/api/linkedin/scan", "/api/hellowork/scan", "/api/pipeline/run"):
        r = real.post(path, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path


def test_real_app_cors_and_guard_share_the_allow_list() -> None:
    from src.api.app import ALLOWED_WEB_ORIGINS, app

    guards = [m for m in app.user_middleware if m.cls is OriginGuardMiddleware]
    assert len(guards) == 1
    assert list(guards[0].kwargs["allowed_origins"]) == list(ALLOWED_WEB_ORIGINS)
