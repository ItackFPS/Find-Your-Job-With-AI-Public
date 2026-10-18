r"""Process-level write isolation for the lab and its agent (ADR 0011), enforced with a CPython audit hook.

After :func:`install_write_guard`, in THIS process:

* a file inside ``project_root`` may be opened for writing (``open``/``os.open`` with a write mode or
  flag), created, renamed onto, removed, truncated or re-permissioned only under a ``writable`` root
  (the lab's own ``lab.db`` and bundle store); everything else in the repository — ``data/jobs.db``,
  ``config/`` (the active profile and production configuration), ``src/``, ``.github/`` — is refused;
* ``sqlite3.connect`` to a database inside ``project_root`` is refused unless it is a writable root
  or a read-only URI (``file:...?mode=ro``), so ``jobs.db`` can be read but never written; every
  connection refuses ``ATTACH`` (and so ``VACUUM INTO``), which SQLite would perform without any
  Python audit event, and loading extensions;
* path aliases are resolved before checking: ``..``, case, device prefixes (``\\?\``), junctions/symlinks, and any
  other spelling of a directory that is the project root (same file identity); a hard link to a file
  inside the project cannot be created, and a write to an alias of a protected database is refused;
* no subprocess can be started (it would escape the hook), including ``_winapi.CreateProcess``;
* network connections and name lookups are refused except to loopback (a local LLM server): no implicit
  cloud call. ``loopback_ports`` narrows loopback to those ports (the lab CLI allows none, so it cannot
  reach the application's own API and change state through it).

Audit hooks cannot be removed, so a guarded process stays guarded. Violations raise
:class:`GuardViolation` (a ``PermissionError``) before the operation happens. Paths outside
``project_root`` (system temp dirs) are not restricted. This guards the lab's own code paths and any
code it calls — they run no dynamic code (candidates are data) — against writing production state by
mistake. It is not a sandbox: code running inside the process can switch it off with ordinary Python
(e.g. emptying ``_state``, or ``Connection.set_authorizer(None)`` before ``ATTACH``), as can native
code. A process that never installs it (an agent's own shell, the human ``approve`` command) is not
restricted at all; containing such an agent needs an operating-system boundary (ADR 0011).
"""

from __future__ import annotations

import ipaddress
import os
import sys
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PATH_EVENTS = {          # audit event -> indexes of the path arguments that would be modified
    "os.remove": (0,), "os.rmdir": (0,), "os.rename": (0, 1), "os.truncate": (0,), "os.chmod": (0,),
    "os.mkdir": (0,), "os.symlink": (1,), "os.link": (1,), "shutil.rmtree": (0,), "shutil.copyfile": (1,),
    "shutil.copytree": (1,), "shutil.move": (0, 1), "os.utime": (0,), "os.chown": (0,), "os.chflags": (0,),
    "os.setxattr": (0,), "os.removexattr": (0,),
}
_PROCESS_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
                   "os.fork", "os.forkpty", "_winapi.CreateProcess")
# _winapi.CreateFile: access rights and creation dispositions that write, create, delete or re-permission
_WIN_WRITE_ACCESS = (0x40000000 | 0x10000000 | 0x2 | 0x4 | 0x10 | 0x100 | 0x10000 | 0x40000 | 0x80000)
_WIN_CREATING_DISPOSITIONS = (1, 2, 4, 5)       # CREATE_NEW, CREATE_ALWAYS, OPEN_ALWAYS, TRUNCATE_EXISTING
_SQLITE_EVENTS_REFUSED = ("sqlite3.enable_load_extension", "sqlite3.load_extension")
_LOOPBACK_NAMES = {"localhost"}

_state: dict[str, Any] = {}
_local = threading.local()


class GuardViolation(PermissionError):
    pass


def _strip_device_prefix(raw: str) -> str:
    r"""``\\?\C:\x`` / ``\\.\C:\x`` → ``C:\x`` and ``\\?\UNC\h\s`` → ``\\h\s`` (Windows), so a prefixed spelling of a
    project path is still recognised as inside the project."""
    for prefix in ("\\\\?\\UNC\\", "//?/UNC/"):
        if raw[:len(prefix)].upper() == prefix.upper():
            return "\\\\" + raw[len(prefix):]
    for prefix in ("\\\\?\\", "\\\\.\\", "//?/", "//./"):
        rest = raw[len(prefix):]
        # only a drive path: other devices (``\\.\pipe\x``, ``\\.\PhysicalDrive0``) are not files in the project
        if raw.startswith(prefix) and len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
            return rest
    return raw


def _identity(p: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _resolve(p: Any) -> Path | None:
    """The real path ``p`` designates: absolute, with links/junctions resolved and, when it is spelled through
    another name of the project root (a UNC share, a short 8.3 name, a substituted drive...), re-rooted under
    the project root by file identity."""
    if isinstance(p, int):                   # a file descriptor: nothing to resolve
        return None
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    if not isinstance(p, (str, os.PathLike)):
        return None
    raw = _strip_device_prefix(os.fspath(p))
    try:
        path = Path(os.path.abspath(raw)).resolve()
    except (OSError, ValueError):
        path = Path(os.path.abspath(raw))
    if not _state or _inside(path, _state["root"]) or _state.get("root_id") is None:
        return path
    root: Path = _state["root"]
    for anc in (path, *path.parents):
        if _identity(anc) == _state["root_id"]:
            return root / path.relative_to(anc)
    return path


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _check_path(p: Any, what: str) -> None:
    path = _resolve(p)
    if path is None:
        return
    if not _inside(path, _state["root"]):
        ident = _identity(path)              # e.g. a hard link to jobs.db made before the guard
        if ident is not None and ident in _state["protected_ids"]:
            raise GuardViolation(f"lab guard: {what} {path} is another name of a protected file")
        return
    if any(_inside(path, w) for w in _state["writable"]):
        return
    raise GuardViolation(f"lab guard: {what} {path} is outside the lab's writable roots")


def _sqlite_target(database: Any) -> tuple[Any, bool]:
    """``(path, read_only)`` of a ``sqlite3.connect`` target (``str``, ``bytes`` or ``os.PathLike``)."""
    if isinstance(database, os.PathLike):
        return database, False                   # a path object is never a URI: always read-write
    if isinstance(database, bytes):
        database = os.fsdecode(database)
    if not isinstance(database, str) or database in (":memory:", ""):
        return None, True
    if database.startswith("file:"):
        parts = urlsplit(database)
        params = dict(kv.split("=", 1) for kv in parts.query.split("&") if "=" in kv)
        if params.get("mode") == "memory":
            return None, True
        raw = unquote(parts.path)
        if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]                        # file:/C:/x.db
        return raw, params.get("mode") == "ro"
    return database, False


def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    if not isinstance(host, str):
        return False                             # unix sockets etc.: not expected in the lab
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False                             # a hostname that is not "localhost": refused


def _sqlite_authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, source: Any) -> int:
    import sqlite3

    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


def _restrict_connection(conn: Any) -> None:
    """``ATTACH`` (and ``VACUUM INTO``, which attaches its output) opens files inside SQLite, where no audit
    event fires: refuse it on every connection the guarded process makes."""
    import sqlite3

    conn.set_authorizer(_sqlite_authorizer)
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)


def _guarded_connect(*args: Any, **kwargs: Any) -> Any:
    """``sqlite3.connect`` while guarded: the connection is restricted before the caller can use it. The audit
    hook refuses any connection opened another way (``sqlite3.Connection(...)``, a ``connect`` captured
    before the guard), since those would not be restricted."""
    _local.connecting = True
    try:
        conn = _state["sqlite_connect"](*args, **kwargs)
    finally:
        _local.connecting = False
    _restrict_connection(conn)
    return conn


def _wrap_sqlite_connect() -> None:
    import sqlite3
    import sqlite3.dbapi2

    _state["sqlite_connect"] = sqlite3.connect
    sqlite3.connect = _guarded_connect      # type: ignore[assignment]
    sqlite3.dbapi2.connect = _guarded_connect   # type: ignore[assignment]


def _is_allowed_peer(address: Any) -> bool:
    if not _is_loopback(address):
        return False
    ports = _state.get("loopback_ports")
    if ports is None:
        return True
    port = address[1] if isinstance(address, tuple) and len(address) >= 2 else None
    return port in ports


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if not _state:
        return
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
        if writes:
            _check_path(path, "write to")
        return
    if event in _PATH_EVENTS:
        for i in _PATH_EVENTS[event]:
            if i < len(args):
                _check_path(args[i], f"{event} on")
        if event == "os.link" and args:      # a hard link is a second, writable name of the same file
            _check_path(args[0], "hard link to")
        return
    if event == "_winapi.CreateFile" and len(args) >= 4:
        access, disposition = args[1], args[3]
        if (isinstance(access, int) and access & _WIN_WRITE_ACCESS) or disposition in _WIN_CREATING_DISPOSITIONS:
            _check_path(args[0], "write handle to")
        return
    if event == "_winapi.CreateJunction" and len(args) >= 2:
        _check_path(args[0], "junction to")
        _check_path(args[1], "junction at")
        return
    if event == "sqlite3.connect":
        if not getattr(_local, "connecting", False):
            raise GuardViolation("lab guard: open SQLite databases with sqlite3.connect (unrestricted connection)")
        target, read_only = _sqlite_target(args[0] if args else None)
        if target is not None and not read_only:
            _check_path(target, "read-write database connection to")
        return
    if event in _SQLITE_EVENTS_REFUSED:
        raise GuardViolation(f"lab guard: SQLite extensions are not allowed ({event})")
    if event.startswith(_PROCESS_EVENTS):
        raise GuardViolation(f"lab guard: starting a process is not allowed ({event})")
    if event == "socket.sendmsg" and len(args) >= 2 and args[1] is not None and not _is_allowed_peer(args[1]):
        raise GuardViolation(f"lab guard: network access to {args[1]!r} is not allowed (loopback only)")
    if event in ("socket.connect", "socket.sendto") and len(args) >= 2 and not _is_allowed_peer(args[1]):
        raise GuardViolation(f"lab guard: network access to {args[1]!r} is not allowed (loopback only)")
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr") and args \
            and args[0] is not None and not _is_loopback(os.fsdecode(args[0]) if isinstance(args[0], bytes) else args[0]):
        raise GuardViolation(f"lab guard: resolving {args[0]!r} is not allowed (loopback only)")
    if event == "socket.getnameinfo" and args and not _is_loopback(args[0]):
        raise GuardViolation(f"lab guard: resolving {args[0]!r} is not allowed (loopback only)")


def protected_paths(project_root: str | Path, jobs_db: str | Path | None = None) -> list[Path]:
    """What a writable root may never be, contain or sit inside: the production database (with its SQLite
    side files), the configuration, the code and the repository metadata."""
    root = Path(project_root).resolve()
    dbs = [root / "data" / "jobs.db", *([Path(jobs_db).resolve()] if jobs_db is not None else [])]
    out = [root / name for name in ("config", "src", "tests", "frontend", ".git", ".github")]
    for db in dbs:
        out += [db, *(db.with_name(db.name + s) for s in ("-wal", "-shm", "-journal"))]
    return out


def install_write_guard(
    project_root: str | Path, writable: Iterable[str | Path], *, jobs_db: str | Path | None = None,
    loopback_ports: Iterable[int] | None = None,
) -> None:
    """Restrict this process for the rest of its life (idempotent for the same arguments).

    A writable root that is, contains or lies inside a :func:`protected_paths` entry (e.g. ``--lab-db
    data/jobs.db`` or ``--store config``) is refused before anything is installed. ``loopback_ports``:
    ``None`` allows any loopback port; otherwise only those (``()``: no network at all)."""
    root = Path(project_root).resolve()
    roots = tuple(Path(w).resolve() for w in writable)
    ports = None if loopback_ports is None else frozenset(loopback_ports)
    protected = protected_paths(root, jobs_db)
    for w in roots:
        if w == root or any(_inside(w, p) or _inside(p, w) for p in protected):
            raise GuardViolation(f"lab guard: {w} may not be writable (production data, configuration or code)")
    if _state:
        if _state["root"] != root or set(_state["writable"]) != set(roots) or _state["loopback_ports"] != ports:
            raise GuardViolation("lab guard already installed with different roots")
        return
    sys.dont_write_bytecode = True               # no .pyc writes into src/ while guarded
    protected_ids = frozenset(i for i in (_identity(p) for p in protected if p.is_file()) if i is not None)
    _state.update(root=root, writable=roots, loopback_ports=ports, root_id=_identity(root),
                  protected_ids=protected_ids)
    _wrap_sqlite_connect()
    sys.addaudithook(_hook)


def guard_installed() -> bool:
    return bool(_state)


def lab_writable_roots(lab_db: str | Path, store_dir: str | Path) -> list[Path]:
    """The lab database (with its SQLite side files) and the bundle store — nothing else."""
    db = Path(lab_db)
    return [db, *(db.with_name(db.name + s) for s in ("-wal", "-shm", "-journal")), Path(store_dir)]
