"""Serve the local API only to processes of the OS account that runs it (opt-in: ``API_SAME_USER_ONLY=1``).

The Host and Origin guards (``origin_guard.py``) trust every process on this machine. That is right while
one person uses it, and wrong once an automated lab agent runs under its own, less privileged account
(ADR 0011, "Promotion authority"): file permissions keep that account out of ``jobs.db`` and ``config/``,
but it could still ``PUT /api/profile`` or ``POST /api/pipeline/run`` and this server — running as the
human — would make the write for it.

Loopback TCP carries no credentials, but the kernel records which process owns the client end of every
connection. This middleware looks that owner up for each request (Windows: ``GetExtendedTcpTable`` and
the owning process's token; Linux: the ``uid`` column of ``/proc/net/tcp*``) and refuses the request
unless it is the account this server runs as. It fails closed: a peer that is not on loopback, an owner
that cannot be found or read (a process of another account usually cannot even be opened), a connection
that matches more than one owner, or an unsupported platform is refused — the latter at startup.

It refuses every request that carries a forwarding header (``X-Forwarded-For``, ``Forwarded``, …):
uvicorn by default trusts ``X-Forwarded-For`` from loopback and rewrites the client address — port
included — before any middleware runs, so a process of another account could name the client port of
one of the human's open connections (any account can list them) and be looked up as that connection.

What it cannot see: a process of the SAME account that relays requests (the Vite dev server's proxy, a
browser, Docker Desktop forwarding a containerised n8n) is allowed, and so is whatever talks to it. Such
relays must not be reachable by the restricted account (docs/development.md, *Isolated lab agent*).
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import socket
import sys
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "API_SAME_USER_ONLY"
# isolation-check (src/lab/isolation.py) recognises a refusal by this header; it keeps its own copy of the
# name because the lab never imports the API (a test checks the two agree)
REFUSAL_HEADER = "x-fyj-peer-guard"
_REFUSAL_BODY = json.dumps({"detail": "This API only serves the OS account that runs it"}).encode()

Address = tuple[str, int]
# headers a server or proxy layer may use to replace the connection's real peer address
FORWARDING_HEADERS = frozenset({b"x-forwarded-for", b"forwarded", b"x-real-ip", b"true-client-ip",
                                b"x-client-ip", b"x-forwarded-host", b"x-forwarded-port"})


def enabled_from_env(environ: Any = os.environ) -> bool:
    return str(environ.get(ENV_VAR, "")).strip().lower() in ("1", "true", "yes", "on")


# -- who owns the client end of a loopback connection -----------------------------------------------------

if sys.platform == "win32":
    from ctypes import wintypes

    _AF_INET, _AF_INET6 = 2, 23
    _TCP_TABLE_OWNER_PID_ALL = 5
    _ERROR_INSUFFICIENT_BUFFER = 122
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    class _Row4(ctypes.Structure):
        _fields_ = [("state", wintypes.DWORD), ("local_addr", ctypes.c_ubyte * 4), ("local_port", wintypes.DWORD),
                    ("remote_addr", ctypes.c_ubyte * 4), ("remote_port", wintypes.DWORD), ("pid", wintypes.DWORD)]

    class _Row6(ctypes.Structure):
        _fields_ = [("local_addr", ctypes.c_ubyte * 16), ("local_scope", wintypes.DWORD),
                    ("local_port", wintypes.DWORD), ("remote_addr", ctypes.c_ubyte * 16),
                    ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
                    ("state", wintypes.DWORD), ("pid", wintypes.DWORD)]

    _iphlpapi = ctypes.WinDLL("iphlpapi")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                              wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    _iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

    def tcp_rows(family: int) -> list[tuple[Address, Address, int]]:
        """``(local, remote, pid)`` for every TCP endpoint of this address family (all processes, all states)."""
        af, row_type = (_AF_INET, _Row4) if family == socket.AF_INET else (_AF_INET6, _Row6)
        size = wintypes.DWORD(0)
        for _ in range(5):          # the table can grow between the size query and the copy
            buf = ctypes.create_string_buffer(size.value or 1)
            rc = _iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, af, _TCP_TABLE_OWNER_PID_ALL, 0)
            if rc == 0:
                break
            if rc != _ERROR_INSUFFICIENT_BUFFER:
                raise OSError(f"GetExtendedTcpTable failed ({rc})")
        else:
            raise OSError("GetExtendedTcpTable: table kept growing")
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        offset = ctypes.sizeof(wintypes.DWORD)          # dwNumEntries, then DWORD-aligned rows
        rows = (row_type * count).from_buffer_copy(buf.raw[offset:offset + count * ctypes.sizeof(row_type)])
        packed = 4 if family == socket.AF_INET else 16

        def addr(raw: Any, port: int) -> Address:
            return str(ipaddress.ip_address(bytes(raw[:packed]))), socket.ntohs(port & 0xFFFF)

        return [(addr(r.local_addr, r.local_port), addr(r.remote_addr, r.remote_port), int(r.pid)) for r in rows]

    def process_owner(pid: int) -> str | None:
        """The SID string of the account running ``pid``, or ``None`` when it cannot be read."""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid > 4 else None
        if not handle:
            return None
        try:
            return _token_user(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def _token_user(process: Any) -> str | None:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value)
            if not needed.value or not _advapi32.GetTokenInformation(token, _TOKEN_USER, buf, needed,
                                                                     ctypes.byref(needed)):
                return None
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]        # TOKEN_USER.User.Sid
            text = ctypes.c_void_p()
            if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)) or not text.value:
                return None
            try:
                return ctypes.wstring_at(text.value)
            finally:
                _kernel32.LocalFree(text)
        finally:
            _kernel32.CloseHandle(token)

    def current_owner() -> str | None:
        return _token_user(_kernel32.GetCurrentProcess())

    def connection_owner(client: Address, server: Address) -> str | None:
        """The account owning the client end ``client`` of a connection to ``server`` on this machine.

        Both tables are searched: a dual-stack server sees an IPv4 client as ``::ffff:127.0.0.1`` while
        the client's own end is listed in the IPv4 table."""
        pids = {pid for family in (socket.AF_INET, socket.AF_INET6) for local, remote, pid in tcp_rows(family)
                if local[1] == client[1] and _same_ip(local[0], client[0])
                and remote[1] == server[1] and _same_ip(remote[0], server[0])}
        owners = {process_owner(pid) for pid in pids}
        return owners.pop() if len(owners) == 1 else None          # none, or ambiguous: unattributable

elif sys.platform.startswith("linux"):

    def _proc_addr(text: str) -> Address:
        hexaddr, hexport = text.split(":")
        raw = bytes.fromhex(hexaddr)
        # /proc prints each 32-bit word of the address in host byte order
        words = b"".join(raw[i:i + 4][::-1] if sys.byteorder == "little" else raw[i:i + 4]
                         for i in range(0, len(raw), 4))
        return str(ipaddress.ip_address(words)), int(hexport, 16)

    def connection_owner(client: Address, server: Address) -> str | None:
        owners: set[str] = set()
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, encoding="ascii") as fh:
                    lines = fh.read().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                local, remote = _proc_addr(fields[1]), _proc_addr(fields[2])
                if _same_ip(local[0], client[0]) and local[1] == client[1] and remote[1] == server[1] \
                        and _same_ip(remote[0], server[0]):
                    owners.add(f"uid:{fields[7]}")
        return owners.pop() if len(owners) == 1 else None          # none, or ambiguous: unattributable

    def current_owner() -> str | None:
        return f"uid:{os.geteuid()}"

else:
    connection_owner: Callable[[Address, Address], str | None] | None = None
    current_owner: Callable[[], str | None] | None = None


def _canonical_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _same_ip(a: str, b: str) -> bool:
    ca, cb = _canonical_ip(a), _canonical_ip(b)
    return ca is not None and ca == cb


def _is_loopback(host: str) -> bool:
    ip = _canonical_ip(host)
    return ip is not None and ip.is_loopback


def peer_is_owner(scope: Scope, owner_of: Callable[[Address, Address], str | None],
                  self_owner: str) -> bool:
    """True only when the request's peer is a loopback process of the account ``self_owner`` (fails closed)."""
    client, server = scope.get("client"), scope.get("server")
    if not client or not server or not _is_loopback(str(client[0])):
        return False
    if any(name.lower() in FORWARDING_HEADERS for name, _ in scope.get("headers") or ()):
        return False                      # the client address may not be the connection's (module docstring)
    try:
        owner = owner_of((str(client[0]), int(client[1])), (str(server[0]), int(server[1])))
    except (OSError, ValueError):
        return False
    return owner is not None and owner == self_owner


def is_same_account_peer(scope: Scope) -> bool:
    """Whether this request comes from a process of the account running this server, on loopback.

    Independent of ``API_SAME_USER_ONLY``: used to decide what a response may disclose (the launcher's
    instance token in ``/api/health``). False wherever the peer's account cannot be identified."""
    if connection_owner is None or current_owner is None:
        return False
    try:
        me = current_owner()
    except OSError:
        return False
    return bool(me) and peer_is_owner(scope, connection_owner, str(me))


# -- the middleware ----------------------------------------------------------------------------------------

class SameUserPeerGuardMiddleware:
    """Refuse every HTTP/WebSocket request whose loopback peer process is not this server's own account."""

    def __init__(self, app: ASGIApp, enabled: bool = False,
                 owner_of: Callable[[Address, Address], str | None] | None = None,
                 self_owner: str | None = None) -> None:
        self.app = app
        self.enabled = enabled
        if not enabled:
            return
        lookup = owner_of or connection_owner
        me = self_owner if self_owner is not None else (current_owner() if current_owner is not None else None)
        if lookup is None or not me:
            raise RuntimeError(f"{ENV_VAR} is set but this platform ({sys.platform}) cannot identify the account "
                               "of a local peer: refusing to start rather than serve every local account")
        self.owner_of: Callable[[Address, Address], str | None] = lookup
        self.self_owner: str = me

    def allowed(self, scope: Scope) -> bool:
        return peer_is_owner(scope, self.owner_of, self.self_owner)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] not in ("http", "websocket") or self.allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (REFUSAL_HEADER.encode(), b"refused"),
                                (b"content-length", str(len(_REFUSAL_BODY)).encode())]})
        await send({"type": "http.response.body", "body": _REFUSAL_BODY})
