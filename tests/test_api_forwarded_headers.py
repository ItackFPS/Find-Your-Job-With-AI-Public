"""Forwarding headers cannot get a request past the local-only guards.

uvicorn trusts ``X-Forwarded-For``/``-Proto`` from loopback by default and
rewrites the client address and scheme. In the default configuration
(``API_SAME_USER_ONLY`` off, so the peer guard that refuses these headers is
not active) nothing may depend on what they say: the Host guard (DNS
rebinding) reads the raw ``Host`` header and the write guard reads ``Origin``.

The app here is the real production app object wrapped exactly like uvicorn
wraps it, with every peer trusted (worst case). Requests are refused before
any route runs, so no database or config is touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.app import app

_FORGED = {
    "X-Forwarded-For": "127.0.0.1",
    "X-Forwarded-Host": "127.0.0.1:8000",
    "X-Forwarded-Proto": "http",
    "Forwarded": "for=127.0.0.1;host=127.0.0.1:8000;proto=http",
    "X-Real-IP": "127.0.0.1",
}


def _client(host: str) -> TestClient:
    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"), base_url=f"http://{host}")


@pytest.mark.parametrize("path", ["/api/jobs", "/api/runs", "/"])
def test_forwarded_headers_do_not_make_a_rebinding_host_local(path):
    resp = _client("attacker.example:8000").get(path, headers=_FORGED)
    assert resp.status_code == 400


def test_forwarded_headers_do_not_make_a_foreign_origin_allowed():
    resp = _client("127.0.0.1:8000").post(
        "/api/linkedin/scan", headers={**_FORGED, "Origin": "http://attacker.example"},
    )
    assert resp.status_code == 403
