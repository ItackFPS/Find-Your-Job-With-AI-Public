"""Can THIS operating-system account write production state? The check behind the lab's real boundary.

The in-process guard (``src/lab/guard.py``) is not a sandbox, and an automated agent with a shell runs as
whatever account started it. The boundary that holds against such an agent is the operating system's:
run it under an account that cannot write ``jobs.db``, ``config/``, the code, credentials or deployment
files (ADR 0011, "Promotion authority"). This module verifies that setup from inside that account; it
cannot create or configure it.

Probes never change content: an existing file is opened read-write and closed without writing; a
directory gets a uniquely named empty probe file that is removed at once (no ``.py`` suffix, so no
auto-reloader reacts). A probe that is refused for any reason counts as protected.

File permissions are not the whole boundary, so the full check also probes:

* secrets (``.env*`` except ``.env.example``, ``config/*.local.*``): a readable one is the account's
  too — ``.env`` holds e.g. the n8n API key, and n8n can call the API as the human;
* the application's API port: the API runs as the human and writes production state for any caller, so
  it must refuse this account (``API_SAME_USER_ONLY=1``, ``src/api/peer_guard.py``). Nothing listening
  proves nothing (the server may start later without the guard, or this account could take the port),
  so it counts as unverified;
* relay ports (the Vite dev server, n8n): services of the human's account that forward requests to the
  API. Anything answering there is a way round the API guard; nothing listening is fine;
* links (junctions, symlinks) inside the repository that lead outside it: whatever is read through
  them is whatever sits at the target, so a target this account can write — or, when it is missing,
  create — lets it plant content (e.g. agent skills) that the human's tools load;
* places the human executes from, outside the repository (``exec_paths``: PATH directories, scheduled
  tasks, Startup items — ``scripts/lab_isolation.ps1 -Mode Audit``, run by the human, lists them):
  one this account can write, or create, runs its code as the human, which defeats everything above;
* membership of Administrators (enabled or UAC-filtered): such an account can rewrite any ACL.

Inside the lab's AppContainer (``src/lab/sandbox.py``) the same probes run, with two readings that
only the container makes true: the OS refuses its loopback connections (no administrator has exempted
it), so a port that does not answer is ``blocked``, not unverified; and only an *enabled*
Administrators membership counts, because the container's token keeps that group deny-only.

Each network probe is one ``GET /`` to loopback.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
from pathlib import Path
from typing import Any

from src.lab.sandbox import in_app_container

PROBE_SUFFIX = ".isolation-probe"
API_PORTS = (8000,)
RELAY_PORTS = (5173, 5678)                 # Vite dev proxy, n8n
# written by the launcher (src/launcher/core.py Settings.instance_file / port_file); the lab never imports it
INSTANCE_LOCK = Path("data") / "state" / "instance.json"
LIVE_PORT_FILE = Path("data") / "state" / "api-port.json"
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
# the API's same-account guard marks its refusals with this header (``src/api/peer_guard.py``
# REFUSAL_HEADER; the lab never imports the API, a test checks that the two agree)
PEER_GUARD_HEADER = "x-fyj-peer-guard"


def _file_writable(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    os.close(fd)
    return True


def _file_readable(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    os.close(fd)
    return True


def _dir_writable(path: Path) -> bool:
    # not tempfile.mkstemp: on Windows it retries a PermissionError while os.access() (which ignores ACLs)
    # says the directory is writable — up to TMP_MAX (2**31) times, so an ACL-denied directory hangs it
    for _ in range(3):
        name = os.path.join(path, f".fyj-{secrets.token_hex(8)}{PROBE_SUFFIX}")
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            break
        except FileExistsError:
            continue
        except OSError:
            return False
    else:
        return False
    os.close(fd)
    try:
        os.remove(name)
    except OSError:
        pass
    return True


def secret_files(root: Path, config_dir: Path) -> list[Path]:
    """Credential files this repository uses: ``.env*`` (not the example), local config overrides and the
    running app's instance lock (it holds the launcher's instance token; the launcher writes it owner-only)."""
    found = [p for p in root.glob(".env*") if p.is_file() and p.name != ".env.example"]
    if config_dir.is_dir():
        found += [p for p in config_dir.glob("*.local.*") if p.is_file()]
    if (root / INSTANCE_LOCK).is_file():
        found.append(root / INSTANCE_LOCK)
    return sorted(found)


def live_api_ports(root: Path) -> tuple[int, ...]:
    """The running app's API port, read from the port file its launcher writes next to the lock
    (``src/launcher/core.py`` ``Settings.port_file``): the launcher picks a free port at each start, so
    the fixed ``API_PORTS`` alone would leave the live one unprobed. Empty when no app is running."""
    try:
        port = int(json.loads((root / LIVE_PORT_FILE).read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return ()
    return (port,) if 0 < port < 65536 else ()


def _probe_one(host: str, port: int, timeout: float) -> str:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/", headers={"Host": f"[{host}]:{port}" if ":" in host else f"{host}:{port}"})
        resp = conn.getresponse()
        return "refused" if resp.status == 403 and resp.getheader(PEER_GUARD_HEADER) == "refused" else "reachable"
    except ConnectionRefusedError:
        return "not_listening"
    except (OSError, http.client.HTTPException, socket.timeout):
        return "unverified"
    finally:
        conn.close()


def probe_port(port: int, timeout: float = 3.0) -> str:
    """``refused`` (the API's same-account guard turned this account away), ``not_listening``,
    ``reachable`` (something answered this account) or ``unverified`` (no clear answer), over both
    loopback addresses: a service may listen on one only (Node binds ``localhost`` to ``::1``, so the
    Vite dev server is often invisible on 127.0.0.1). The worst answer wins."""
    states = {_probe_one(host, port, timeout) for host in LOOPBACK_HOSTS}
    for state in ("reachable", "unverified", "refused"):
        if state in states:
            return state
    return "not_listening"


def check_network(api_ports: tuple[int, ...] = API_PORTS,
                  relay_ports: tuple[int, ...] = RELAY_PORTS) -> tuple[dict[str, str], list[str]]:
    """The probe result per port, and the problems they show (none when the API refuses this account, or
    when this process is in an AppContainer that the OS keeps off loopback)."""
    results: dict[str, str] = {}
    problems: list[str] = []
    contained = in_app_container()

    def probe(port: int) -> str:
        state = probe_port(port)
        # an AppContainer's loopback connections are refused by the OS (connect fails or times out):
        # whatever listens there now or later is out of its reach unless an administrator exempts it
        return "blocked" if contained and state in ("not_listening", "unverified") else state

    for port in api_ports:
        results[str(port)] = state = probe(port)
        if state == "blocked":
            continue
        if state == "reachable":
            problems.append(f"API port {port} serves this account: it can make the API write production state "
                            "(start the API with API_SAME_USER_ONLY=1)")
        elif state != "refused":
            problems.append(f"API port {port}: {state.replace('_', ' ')} - cannot verify that the API refuses "
                            "this account (start it with API_SAME_USER_ONLY=1, then re-check)")
    for port in relay_ports:
        results[str(port)] = state = probe(port)
        if state not in ("not_listening", "refused", "blocked"):
            problems.append(f"relay port {port} ({state.replace('_', ' ')}): a service answers this account and "
                            "may forward to the API as another account - stop it while the agent runs")
    return results, problems


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & 0x400)                                  # FILE_ATTRIBUTE_REPARSE_POINT (junctions)


def outside_links(root: Path) -> list[tuple[Path, Path]]:
    """``(link, resolved target)`` for every link under ``root`` whose target lies outside it (not followed)."""
    real_root = Path(os.path.realpath(root))
    found: list[tuple[Path, Path]] = []
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                link = _is_link(entry)
            except OSError:
                continue
            if link:
                try:
                    target = Path(os.path.abspath(os.path.join(os.path.dirname(entry.path), os.readlink(entry.path))))
                except OSError:
                    continue                                    # not a name-surrogate link (e.g. a dedup file)
                target = Path(str(target).removeprefix("\\\\?\\"))
                if real_root != target and real_root not in target.parents:
                    found.append((Path(entry.path), target))
            elif entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return found


def _subdir_creatable(path: Path) -> bool:
    """Whether this account can create a directory in ``path`` (an empty one, removed at once). On Windows
    "create folders" is a right of its own: a drive root commonly grants it to every user without
    "create files", and the creator owns, so may fill, the folder it makes."""
    for _ in range(3):
        name = os.path.join(path, f".fyj-{secrets.token_hex(8)}{PROBE_SUFFIX}")
        try:
            os.mkdir(name)
            break
        except FileExistsError:
            continue
        except OSError:
            return False
    else:
        return False
    try:
        os.rmdir(name)
    except OSError:
        pass
    return True


def _creatable(target: Path) -> bool:
    """Whether this account can write ``target``, or create it from its nearest existing ancestor."""
    probe = target
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    if not probe.is_dir():
        return _file_writable(probe)
    if _dir_writable(probe):
        return True
    # a missing path several levels down is reached by creating folders first
    return probe != target and probe != target.parent and _subdir_creatable(probe)


def in_administrators() -> bool:
    """Whether this account belongs to BUILTIN\\Administrators, elevated or not (Windows; False elsewhere)."""
    if os.name != "nt":
        return False
    if in_app_container():
        return _admin_enabled()
    import subprocess

    try:
        out = subprocess.run(["whoami", "/groups", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             errors="replace", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return True                                            # cannot tell: fail closed
    return '"S-1-5-32-544"' in out


def _admin_enabled() -> bool:
    """Whether Administrators is an ENABLED group of this process's token (an AppContainer token keeps it
    deny-only, which grants nothing). Fails closed."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32.CreateWellKnownSid.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(wintypes.DWORD)]
    advapi32.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    sid = ctypes.create_string_buffer(68)                   # SECURITY_MAX_SID_SIZE
    size = wintypes.DWORD(len(sid))
    member = wintypes.BOOL(True)
    if not advapi32.CreateWellKnownSid(26, None, sid, ctypes.byref(size)):        # WinBuiltinAdministratorsSid
        return True
    if not advapi32.CheckTokenMembership(None, sid, ctypes.byref(member)):
        return True
    return bool(member.value)


def protected_targets(root: Path, jobs_db: Path, config_dir: Path) -> list[tuple[str, Path]]:
    """``(kind, path)`` for every existing production target: ``file`` targets and ``dir`` targets."""
    out: list[tuple[str, Path]] = []
    for db in (jobs_db, Path(f"{jobs_db}-wal"), Path(f"{jobs_db}-shm")):
        if db.is_file():
            out.append(("file", db))
    if config_dir.is_dir():
        out.append(("dir", config_dir))
        out += [("file", p) for p in sorted(config_dir.rglob("*")) if p.is_file()]
    # labelled datasets are the evidence an approval re-checks: the agent may read them, never edit them
    if (root / "data" / "benchmarks").is_dir():
        out.append(("dir", root / "data" / "benchmarks"))
    # the data directory itself: a file planted there next to jobs.db (a ``jobs.db-wal`` or ``-journal``
    # while the database is closed) is replayed INTO it at the next open
    if jobs_db.parent.is_dir():
        out.append(("dir", jobs_db.parent))
    # the checkout itself and everything at its top: a module or ``__pycache__`` planted at the root or in
    # any package shadows the real one, ``.git`` holds hooks, ``.claude``/``.mcp.json``/``AGENTS.md`` steer
    # your agents, ``.venv`` is your interpreter — each runs as you. Only ``data`` is checked above.
    out.append(("dir", root))
    for p in sorted(root.iterdir()):
        if p.name == "data" or p.is_symlink():
            continue
        if p.is_dir() and ("dir", p) not in out:
            out.append(("dir", p))
        elif p.is_file() and ("file", p) not in out:
            out.append(("file", p))
    return out


def check_isolation(root: str | Path, jobs_db: str | Path, config_dir: str | Path, *,
                    probe_dirs: bool = True, lab_dir: str | Path | None = None,
                    api_ports: tuple[int, ...] = API_PORTS,
                    relay_ports: tuple[int, ...] = RELAY_PORTS,
                    exec_paths: tuple[str | Path, ...] = ()) -> dict[str, Any]:
    """What this account can write or read of production, and whether the API refuses it.

    ``os_enforced`` only when it can write no protected target and read no secret, the API port answers
    with the same-account guard's refusal and no relay port answers. ``probe_dirs=False`` checks existing
    files only (writes nothing, opens no connection) and can then never report ``os_enforced``: a
    writable directory lets an account replace the files in it."""
    root, jobs_db, config_dir = Path(root), Path(jobs_db), Path(config_dir)
    writable = [str(p) for kind, p in protected_targets(root, jobs_db, config_dir)
                if (_file_writable(p) if kind == "file" else probe_dirs and _dir_writable(p))]
    secrets = [str(p) for p in secret_files(root, config_dir) if _file_readable(p)]
    notes = [] if probe_dirs else ["directories and network not probed (files only): run isolation-check for the full check"]
    network: dict[str, str] = {}
    problems: list[str] = []
    if probe_dirs:
        network, problems = check_network(api_ports, relay_ports)
        if in_administrators():
            problems.append("this account belongs to Administrators: it can rewrite any ACL")
        for place in map(Path, exec_paths):
            if _creatable(place) or (place.is_file() and _dir_writable(place.parent)):
                problems.append(f"{place} is executed by the human and writable (or creatable) by this account: "
                                "code planted there runs as the human")
        for link, target in outside_links(root):
            if _creatable(target):
                state = "writable" if target.exists() else "missing and creatable"
                problems.append(f"{link} leads outside the repository to {target}, {state} by this account: it "
                                "could plant what is read through the link")
        lab =Path(lab_dir) if lab_dir is not None else jobs_db.parent / "lab"
        if not (lab.is_dir() and _dir_writable(lab)):
            notes.append(f"{lab} is missing or not writable by this account: the isolated agent keeps its lab "
                         "database there (--lab-db data/lab/lab.db)")
        if (jobs_db.parent / "lab.db").is_file():
            notes.append(f"{jobs_db.parent / 'lab.db'} exists: the Lab page shows it instead of "
                         f"{jobs_db.parent / 'lab' / 'lab.db'} (move it there as its owner)")
    if not jobs_db.is_file():
        notes.append(f"{jobs_db} not found: nothing checked for it")
    return {"os_enforced": probe_dirs and not writable and not secrets and not problems,
            "writable": writable, "readable_secrets": secrets, "network": network,
            "problems": problems, "notes": notes}
