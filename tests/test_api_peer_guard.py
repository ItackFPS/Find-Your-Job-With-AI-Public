"""API_SAME_USER_ONLY: the local API serves only processes of the OS account that runs it (ADR 0011).

A lab agent under its own account must not be able to make the API — which runs as the human — write
production state for it. The guard asks the OS who owns the client end of each loopback connection and
fails closed on anything it cannot attribute to the server's own account.
"""

from __future__ import annotations

import socket
import sys
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import peer_guard
from src.api.peer_guard import REFUSAL_HEADER, SameUserPeerGuardMiddleware, enabled_from_env

SUPPORTED = sys.platform == "win32" or sys.platform.startswith("linux")


def _app(**guard) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    calls: list[str] = []

    @app.put("/api/profile")
    def profile() -> dict:
        calls.append("profile")
        return {"ok": True}

    app.add_middleware(SameUserPeerGuardMiddleware, **guard)
    return app, calls


def _owner_is(owner):
    def owner_of(client, server):
        if isinstance(owner, Exception):
            raise owner
        return owner
    return owner_of


def test_disabled_by_default_serves_everyone():
    app, calls = _app()
    assert TestClient(app).put("/api/profile").status_code == 200 and calls == ["profile"]


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("ON", True), ("", False), ("0", False),
                                       ("no", False)])
def test_the_switch_is_explicit(value, on):
    assert enabled_from_env({"API_SAME_USER_ONLY": value}) is on


@pytest.mark.parametrize("owner, allowed", [("S-1-5-21-human", True), ("S-1-5-21-lab-agent", False),
                                            (None, False), (OSError("table unavailable"), False)])
def test_only_the_servers_own_account_is_served(owner, allowed):
    app, calls = _app(enabled=True, owner_of=_owner_is(owner), self_owner="S-1-5-21-human")
    resp = TestClient(app, client=("127.0.0.1", 50000)).put("/api/profile")
    if allowed:
        assert resp.status_code == 200 and calls == ["profile"]
    else:
        assert resp.status_code == 403 and resp.headers[REFUSAL_HEADER] == "refused" and calls == []


@pytest.mark.parametrize("client", ["testclient", "192.168.1.20", "172.17.0.2"])
def test_a_peer_that_is_not_on_loopback_is_refused(client):
    app, calls = _app(enabled=True, owner_of=_owner_is("S-1-5-21-human"), self_owner="S-1-5-21-human")
    assert TestClient(app, client=(client, 50000)).put("/api/profile").status_code == 403 and calls == []


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Port"])
def test_a_request_naming_another_peer_is_refused(header):
    # the server may already have replaced the connection's address with the header's (uvicorn does)
    app, calls = _app(enabled=True, owner_of=_owner_is("S-1-5-21-human"), self_owner="S-1-5-21-human")
    resp = TestClient(app, client=("127.0.0.1", 50000)).put("/api/profile", headers={header: "127.0.0.1:1"})
    assert resp.status_code == 403 and calls == []


def test_the_dev_server_origins_are_dropped_when_only_this_account_is_served():
    from src.api.app import ALLOWED_WEB_ORIGINS, DEV_WEB_ORIGINS, allowed_web_origins

    assert allowed_web_origins(False) == DEV_WEB_ORIGINS and allowed_web_origins(True) == ()
    assert ALLOWED_WEB_ORIGINS == allowed_web_origins(enabled_from_env())


def test_an_unsupported_platform_refuses_to_start(monkeypatch):
    monkeypatch.setattr(peer_guard, "connection_owner", None)
    monkeypatch.setattr(peer_guard, "current_owner", None)
    with pytest.raises(RuntimeError, match="refusing to start"):
        SameUserPeerGuardMiddleware(FastAPI(), enabled=True)


def test_the_application_installs_it_outermost():
    from src.api.app import app

    assert app.user_middleware[0].cls is SameUserPeerGuardMiddleware


def test_isolation_check_recognises_its_refusals():
    from src.lab import isolation

    assert isolation.PEER_GUARD_HEADER == REFUSAL_HEADER


# -- against the real OS: real sockets, a real server --------------------------------------------------

@pytest.mark.skipif(not SUPPORTED, reason="peer lookup is implemented for Windows and Linux")
@pytest.mark.parametrize("family, host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_the_os_attributes_a_loopback_connection_to_this_account(family, host):
    try:
        srv = socket.socket(family)
        srv.bind((host, 0))
    except OSError:
        pytest.skip(f"{host} unavailable")
    srv.listen()
    with srv, socket.create_connection(srv.getsockname()[:2]) as cli:
        conn, peer = srv.accept()
        with conn:
            server = srv.getsockname()[:2]
            assert peer_guard.connection_owner(peer[:2], server) == peer_guard.current_owner()
            assert cli.getsockname()[:2] == peer[:2]
            assert peer_guard.connection_owner((host, 1), server) is None          # no such connection


@pytest.mark.skipif(not SUPPORTED, reason="peer lookup is implemented for Windows and Linux")
def test_an_ipv4_client_of_a_dual_stack_server_is_attributed():
    # the server sees ::ffff:127.0.0.1, the client's own end is listed under IPv4
    try:
        srv = socket.socket(socket.AF_INET6)
        srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        srv.bind(("::", 0))
    except OSError:
        pytest.skip("no dual-stack socket here")
    srv.listen()
    with srv, socket.create_connection(("127.0.0.1", srv.getsockname()[1])):
        conn, peer = srv.accept()
        with conn:
            assert peer[0] == "::ffff:127.0.0.1"
            assert peer_guard.connection_owner(peer[:2], conn.getsockname()[:2]) == peer_guard.current_owner()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows TCP table")
def test_a_connection_matching_two_owners_is_unattributable(monkeypatch):
    rows = {socket.AF_INET: [(("127.0.0.1", 5000), ("127.0.0.1", 8000), 101)],
            socket.AF_INET6: [(("::ffff:127.0.0.1", 5000), ("::ffff:127.0.0.1", 8000), 202)]}
    monkeypatch.setattr(peer_guard, "tcp_rows", lambda family: rows[family])
    monkeypatch.setattr(peer_guard, "process_owner", lambda pid: {101: "human", 202: "agent"}[pid])
    assert peer_guard.connection_owner(("127.0.0.1", 5000), ("127.0.0.1", 8000)) is None
    monkeypatch.setattr(peer_guard, "process_owner", lambda pid: "human")
    assert peer_guard.connection_owner(("127.0.0.1", 5000), ("127.0.0.1", 8000)) == "human"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tokens")
def test_a_process_of_another_account_is_not_this_account():
    # RPC endpoint mapper (port 135) runs as NETWORK SERVICE on every Windows machine
    pids = {pid for local, _, pid in peer_guard.tcp_rows(socket.AF_INET) if local[1] == 135 and pid > 4}
    if not pids:
        pytest.skip("no RPC endpoint mapper listening")
    assert all(peer_guard.process_owner(pid) != peer_guard.current_owner() for pid in pids)
    assert peer_guard.process_owner(4) is None                                     # System


@pytest.fixture()
def serve():
    """Run an app under a real uvicorn server on a loopback port; yields its port."""
    import uvicorn

    servers = []

    def start(app) -> int:
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


@pytest.mark.skipif(not SUPPORTED, reason="peer lookup is implemented for Windows and Linux")
def test_end_to_end_the_server_tells_its_own_account_from_another(serve):
    import http.client

    from src.lab.isolation import probe_port

    mine, mine_calls = _app(enabled=True)
    port_mine = serve(mine)
    # a server whose own account is someone else's: this process is then the foreign one
    other, other_calls = _app(enabled=True, self_owner="S-1-5-21-0-0-0-somebody-else")
    port_other = serve(other)

    def put(port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("PUT", "/api/profile")
        resp = conn.getresponse()
        return resp.status, resp.getheader(REFUSAL_HEADER)

    assert put(port_mine) == (200, None) and mine_calls == ["profile"]
    assert put(port_other) == (403, "refused") and other_calls == []
    # and isolation-check reads those answers correctly
    assert probe_port(port_other) == "refused"
    assert probe_port(port_mine) == "reachable"


def test_a_forged_forwarded_for_cannot_borrow_an_open_connection(serve):
    """uvicorn (default settings, as scripts/Start_backend.bat runs it) trusts X-Forwarded-For from loopback
    and rewrites the client address, port included. Any account can list the TCP table, so a foreign
    process could name the client port of the human's open (keep-alive) connection and be looked up as it."""
    import http.client

    human_ports: set[int] = set()
    app, calls = _app(enabled=True, self_owner="human",
                      owner_of=lambda client, server: "human" if client[1] in human_ports else "agent")
    port = serve(app)
    human = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    human.connect()
    human_ports.add(human.sock.getsockname()[1])
    human.request("PUT", "/api/profile")
    assert human.getresponse().status == 200 and calls == ["profile"]
    victim = human.sock.getsockname()[1]
    for forged in (f"127.0.0.1:{victim}", f"[::1]:{victim}", "127.0.0.1"):
        agent = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        agent.request("PUT", "/api/profile", headers={"X-Forwarded-For": forged})
        assert agent.getresponse().status == 403, forged
    assert calls == ["profile"]
