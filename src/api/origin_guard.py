"""Keep the local, unauthenticated API reachable only from the local machine.

Security boundary: the backend trusts any process on this machine (curl, n8n,
scripts — none of them is authenticated), and defends against WEB PAGES the
user visits, which can make the browser send requests to 127.0.0.1. Two
ASGI middlewares, the Host guard outermost:

LocalHostGuardMiddleware (issue #59) — every HTTP request, reads included,
must carry a loopback ``Host`` (``localhost``, ``127.0.0.1``, ``[::1]``, any
port) or one listed in ``extra_hosts`` (``API_ALLOWED_HOSTS``, e.g.
``host.docker.internal`` for a containerised n8n). A DNS-rebinding page
(``attacker.example`` re-resolved to 127.0.0.1) always sends its own name as
Host, so it is refused before any route runs — which also stops it from
reading responses, something CORS cannot do once the page is same-origin.
A request with no Host header at all (HTTP/1.0 tools; browsers always send
one) is not a browser request and is let through.

OriginGuardMiddleware (issue #12) — CORS stops a foreign web page from
*reading* responses, but a browser still *sends* "simple" requests — e.g. a
body-less ``POST /api/linkedin/scan`` to 127.0.0.1 — so any site the user
visits could trigger scans or pipeline runs. This middleware refuses write
methods under ``/api`` whose ``Origin`` header names another site. Comparing
Origin with the request's own Host is only sound because the Host guard has
already restricted Host to the local machine.

Allowed:
  * no ``Origin`` header — curl, n8n, scripts, server-to-server calls;
  * ``Origin`` equal to the request's own scheme and host — the SPA served by
    the backend itself;
  * ``Origin`` in ``allowed_origins`` — the Vite dev server (its proxy keeps
    the browser's Origin).

Implemented as plain ASGI (not BaseHTTPMiddleware) so streaming responses
such as the SSE progress endpoint pass through untouched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_REJECTION_BODY = json.dumps({"detail": "Cross-origin request rejected"}).encode()
_INVALID_HOST_BODY = json.dumps({"detail": "Invalid host header"}).encode()
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# "name" or "name:port", or "[ipv6]" / "[ipv6]:port" — nothing else.
_HOST_HEADER = re.compile(r"(?:\[(?P<ipv6>[0-9a-f:.]+)\]|(?P<name>[^:\[\]]+))(?::\d+)?")


def _hostname(host_header: str) -> str | None:
    """Lower-cased host name of a Host header value, or None if malformed."""
    m = _HOST_HEADER.fullmatch(host_header.strip().lower())
    if m is None:
        return None
    return m.group("ipv6") or m.group("name")


class LocalHostGuardMiddleware:
    def __init__(self, app: ASGIApp, extra_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed_hosts = _LOOPBACK_HOSTS | {
            h.strip().lower() for h in extra_hosts if h.strip()
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = _headers(scope).get("host")
            if host is not None and _hostname(host) not in self.allowed_hosts:
                await _respond(send, 400, _INVALID_HOST_BODY)
                return
        await self.app(scope, receive, send)


class OriginGuardMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(o.rstrip("/").lower() for o in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._is_rejected(scope):
            await _respond(send, 403, _REJECTION_BODY)
            return
        await self.app(scope, receive, send)

    def _is_rejected(self, scope: Scope) -> bool:
        if scope.get("method") not in _WRITE_METHODS:
            return False
        if not str(scope.get("path", "")).startswith("/api"):
            return False
        headers = _headers(scope)
        origin = headers.get("origin")
        if origin is None:
            return False
        origin = origin.rstrip("/").lower()
        if origin in self.allowed_origins:
            return False
        host = headers.get("host")
        own_origin = f"{scope.get('scheme', 'http')}://{host}".lower() if host else None
        return origin != own_origin


async def _respond(send: Send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _headers(scope: Scope) -> dict[str, str]:
    raw: list[tuple[bytes, bytes]] = scope.get("headers") or []
    result: dict[str, Any] = {}
    for name, value in raw:
        result[name.decode("latin-1").lower()] = value.decode("latin-1")
    return result
