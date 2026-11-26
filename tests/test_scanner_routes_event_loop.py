"""Scanner routes do not block the event loop (Phase B B10, P-012).

A route declared ``async def`` runs on the event loop: a synchronous SQLite call inside it stops every
other request until it returns. The HelloWork routes call synchronous staging storage, so
they are plain ``def`` routes, which FastAPI runs in its thread pool. This test serves them on a real
loopback server with a staging read that takes 0.8 s, and checks that another request is answered
meanwhile. No network beyond loopback; no database.
"""

from __future__ import annotations

import http.client
import threading
import time

import pytest
from fastapi import FastAPI

SLOW = 0.8


class _SlowStaging:
    def get_status(self) -> dict:
        time.sleep(SLOW)
        return {"waiting": 0, "last_scan": None}

    def list_scan_runs(self, limit: int = 20) -> list:
        time.sleep(SLOW)
        return []


class _Scanner:
    def is_scanning(self) -> bool:
        return False


def _app() -> FastAPI:
    from src.api.routes import hellowork

    app = FastAPI()
    app.include_router(hellowork.router)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    app.state.hellowork_staging = _SlowStaging()
    app.state.hellowork_scanner = _Scanner()
    return app


@pytest.fixture()
def port():
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(_app(), host="127.0.0.1", port=0, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not (server.started and server.servers) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True
    thread.join(timeout=10)


def _get(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    return conn.getresponse().status


@pytest.mark.parametrize("path", ["/api/hellowork/status", "/api/hellowork/scans"])
def test_a_slow_staging_read_does_not_hold_up_other_requests(port: int, path: str) -> None:
    slow = threading.Thread(target=_get, args=(port, path))
    slow.start()
    time.sleep(0.15)                       # the slow route is now inside its staging read
    started = time.monotonic()
    assert _get(port, "/ping") == 200
    waited = time.monotonic() - started
    slow.join()
    assert waited < SLOW / 2, f"/ping waited {waited:.2f} s behind {path}"
