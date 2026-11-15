"""Compatibility of the same-account guard, on by default (owner decision D5, Phase B B10, P-033).

The real application, wrapped in the guard as ``src/api/app.py`` installs it, served by a real uvicorn
server on a loopback port: the launcher's health probe, the page's reads and writes (same origin) and
the dev server's origin in dev mode all work for this account. A server whose account is someone
else's refuses this process, as it would refuse a lab agent's account. Temporary paths only.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time

import pytest

from src.api.peer_guard import REFUSAL_HEADER, SameUserPeerGuardMiddleware

pytestmark = pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                                reason="peer lookup is implemented for Windows and Linux")


@pytest.fixture()
def serve():
    import uvicorn

    servers = []

    def start(app) -> int:  # noqa: ANN001
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not (server.started and server.servers) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn did not start"
        servers.append((server, thread))
        return server.servers[0].sockets[0].getsockname()[1]

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)


def _request(port: int, method: str, path: str, body: dict | None = None,
             origin: str | None = None) -> tuple[int, str | None, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.getheader(REFUSAL_HEADER), resp.read()


def test_the_launcher_the_page_and_its_writes_reach_the_guarded_app(isolated_app, serve) -> None:  # noqa: ANN001
    from src.launcher.core import health

    port = serve(SameUserPeerGuardMiddleware(isolated_app, enabled=True))
    assert health(port) is not None                                          # the launcher's probe
    status, refused, _ = _request(port, "GET", "/api/jobs")
    assert status == 200 and refused is None                                  # the page's reads
    origin = f"http://127.0.0.1:{port}"
    status, refused, _ = _request(port, "POST", "/api/jobs/no-such-offer/feedback",
                                  {"kind": "save"}, origin=origin)            # a same-origin write
    assert refused is None and status != 403


def test_another_accounts_server_refuses_this_process_with_a_reason(isolated_app, serve) -> None:  # noqa: ANN001
    port = serve(SameUserPeerGuardMiddleware(isolated_app, enabled=True, self_owner="S-1-5-21-0-0-0-somebody-else"))
    status, refused, body = _request(port, "GET", "/api/jobs")
    assert (status, refused) == (403, "refused")
    assert "another account" in json.loads(body)["reason"]


def test_dev_mode_keeps_its_origin_with_the_guard_on() -> None:
    from src.api.app import DEV_WEB_ORIGINS, allowed_web_origins

    assert allowed_web_origins(True, dev_server=True) == DEV_WEB_ORIGINS
