"""Delete all application data: a database-free, resumable procedure (ADR 0015, v5 §4.12).

Erase my activity keeps its state in the database it cleans; Delete all removes that database, so
its state lives in ``DELETE_ALL_PENDING.json`` in the data folder's **parent**, outside everything it
deletes. The file is written atomically (temp file, fsync, rename) before anything is touched and
rewritten at each step. It holds paths, sizes, counts and check results, never content.

States: REQUESTED -> QUIESCED -> INVENTORIED -> FILES_DELETED -> VERIFIED -> COMPLETE, or BLOCKED
(with the file named). Every step reads only the state file and the filesystem.

Launcher-owned files are never in scope: the launcher's instance lock and port file
(``data/state/instance.json``, ``data/state/api-port.json``) and everything under ``logs/launcher/``.
The launcher keeps supervising the app across the restart that finishes a deletion; these files hold
no personal content.

In scope: the whole data folder (``jobs.db`` and its WAL, backups, calibration, settings, lab
databases and ``data/lab/``), the personal configuration (``config/*.local.yaml``: profile and its
backups) and the logs. Tracked configuration defaults stay, so the next start is a first run.
Logs are deleted best-effort: a log file held open by the launcher is listed as skipped, not
blocking (logs hold no personal content).

The lab writers are suspended with a content-free ``LAB_SUSPENDED`` marker in ``data/`` and
``data/lab/`` for the whole procedure; verification checks that no lab database reappeared. At
startup the state file is checked before the database is opened or created; while it exists the
application starts nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.privacy import lab_suspension
from src.privacy.lab_suspension import LAB_MARKER

logger = logging.getLogger(__name__)

PENDING = "DELETE_ALL_PENDING.json"
__all__ = ["LAB_MARKER", "PENDING", "STATES", "claim_auto_restart", "pending", "read_state", "request", "run",
           "state_path"]
STATES = ("REQUESTED", "QUIESCED", "INVENTORIED", "FILES_DELETED", "VERIFIED", "COMPLETE")
BLOCKED = "BLOCKED"
DB_PATTERNS = ("*.db", "*.db-wal", "*.db-shm", "*.sqlite")
# Launcher-owned (src/launcher/core.py Settings: instance_file, port_file, logs_dir). Never deleted.
LAUNCHER_STATE_FILES = ("instance.json", "api-port.json")
LAUNCHER_LOGS_DIR = "launcher"
# Automatic restarts one request may use to get past a locked file (Windows): after that, the
# pending request waits for the user's own restart or retry, so a failure can never loop.
MAX_AUTO_RESTARTS = 1


class Blocked(Exception):
    """A file cannot be deleted now (open elsewhere, no permission) or a check failed."""


class _StopAfter(Exception):
    """Test hook: simulate a crash right after a state was recorded."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def state_path(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve().parent / PENDING


def pending(data_dir: str | Path) -> bool:
    return state_path(data_dir).exists()


def read_state(data_dir: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return                              # Windows cannot open a folder; the rename is durable enough
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def request(db_path: str | Path, config_dir: str | Path, logs_dir: str | Path | None = None) -> dict[str, Any]:
    """Write the state file (REQUESTED) → the state. A pending request is returned unchanged."""
    data_dir = Path(db_path).resolve().parent
    existing = read_state(data_dir)
    if existing is not None:
        return existing
    if (data_dir / ".git").exists() or data_dir == data_dir.parent:
        raise ValueError(f"refusing to delete {data_dir}: not an application data folder")
    state = {
        "erasure_id": str(uuid.uuid4()), "requested_at": _now(),
        "data_dir": str(data_dir), "config_dir": str(Path(config_dir).resolve()),
        "logs_dir": str(Path(logs_dir).resolve()) if logs_dir else str(data_dir.parent / "logs"),
        "state": "REQUESTED", "last_state": "REQUESTED", "attempts": 0, "last_error": None,
        "inventory": None, "skipped": [], "verification": None, "auto_restarts": 0,
    }
    _write(state_path(data_dir), state)
    logger.info("Delete all requested for %s.", data_dir)
    return state


def claim_auto_restart(data_dir: str | Path) -> bool:
    """Record one automatic restart for the pending request → ``False`` when none is left (or nothing
    is pending). Written before the process exits, so a restart that fails again is not retried."""
    path = state_path(data_dir)
    state = read_state(data_dir)
    if state is None or state.get("auto_restarts", 0) >= MAX_AUTO_RESTARTS:
        return False
    state["auto_restarts"] = state.get("auto_restarts", 0) + 1
    _write(path, state)
    return True


def run(data_dir: str | Path, quiesce: Callable[[], None] | None = None,
        stop_after: str | None = None) -> dict[str, Any] | None:
    """Run (or resume) the pending procedure to COMPLETE or BLOCKED → the final state. Uses only the
    state file and the filesystem. ``stop_after`` (tests) aborts right after that state is recorded."""
    path = state_path(data_dir)
    state = read_state(data_dir)
    if state is None:
        return None
    state["attempts"] += 1
    state["state"] = state["last_state"]
    state["last_error"] = None
    _write(path, state)
    proc = _Procedure(path, state, quiesce)
    try:
        for nxt in STATES[STATES.index(state["last_state"]) + 1:]:
            getattr(proc, "step_" + nxt.lower())()
            state["state"] = state["last_state"] = nxt
            if nxt != "COMPLETE":
                _write(path, state)
            if stop_after == nxt:
                raise _StopAfter(nxt)
    except Blocked as exc:
        state["state"] = BLOCKED
        state["last_error"] = str(exc)
        _write(path, state)
        logger.warning("Delete all blocked: %s", exc)
    except _StopAfter:
        pass
    return state


class _Procedure:
    def __init__(self, path: Path, state: dict[str, Any], quiesce: Callable[[], None] | None) -> None:
        self.path = path
        self.state = state
        self.quiesce = quiesce
        self.data = Path(state["data_dir"])
        self.config = Path(state["config_dir"])
        self.logs = Path(state["logs_dir"])
        self.requested = datetime.fromisoformat(state["requested_at"])
        self.kept = {self.data / "state" / name for name in LAUNCHER_STATE_FILES}
        self.kept_logs = self.logs / LAUNCHER_LOGS_DIR

    def _keep_dir(self, d: Path) -> bool:
        """A folder that holds (or leads to) a launcher-owned file."""
        return any(k.is_relative_to(d) for k in self.kept if k.exists())

    def _markers(self) -> list[Path]:
        return lab_suspension.markers(self.data)

    def _data_files(self) -> list[Path]:
        if not self.data.is_dir():
            return []
        markers = set(self._markers())
        return sorted(p for p in self.data.rglob("*")
                      if p.is_file() and p not in markers and p not in self.kept)

    def _config_files(self) -> list[Path]:
        return sorted(self.config.glob("*.local.yaml")) if self.config.is_dir() else []

    def _log_files(self) -> list[Path]:
        if not self.logs.is_dir():
            return []
        return sorted(p for p in self.logs.rglob("*") if p.is_file() and not p.is_relative_to(self.kept_logs))

    # -- steps --

    def step_quiesced(self) -> None:
        # Suspend the lab writers first (v5 lab rule 3), in data/ and, when it exists, data/lab/.
        lab_suspension.suspend(self.data)
        if self.quiesce is not None:
            self.quiesce()

    def step_inventoried(self) -> None:
        def entry(p: Path) -> dict[str, Any]:
            try:
                return {"path": str(p), "size": p.stat().st_size}
            except OSError:
                return {"path": str(p), "size": None}
        self.state["inventory"] = {
            "data": [entry(p) for p in self._data_files()],
            "config": [entry(p) for p in self._config_files()],
            "logs": [entry(p) for p in self._log_files()],
        }

    def step_files_deleted(self) -> None:
        # Re-walk rather than trust the inventory: a file created since is deleted too.
        for p in self._data_files() + self._config_files():
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                raise Blocked(f"file_locked: {p} ({exc.strerror or exc})") from exc
        skipped = []
        for p in self._log_files():
            try:
                p.unlink(missing_ok=True)
            except OSError:
                skipped.append(str(p))       # a log the launcher holds open; no personal content
        self.state["skipped"] = skipped
        if self.data.is_dir():
            markers = set(self._markers())
            for d in sorted((d for d in self.data.rglob("*") if d.is_dir()), key=lambda d: len(d.parts),
                            reverse=True):
                if not any(m.parent == d for m in markers) and not self._keep_dir(d):
                    try:
                        d.rmdir()
                    except OSError as exc:
                        if any(d.iterdir()):
                            raise Blocked(f"file_locked: {d} ({exc.strerror or exc})") from exc

    def step_verified(self) -> None:
        inv = self.state.get("inventory") or {}
        markers = set(self._markers())
        listed = [Path(e["path"]) for key in ("data", "config") for e in inv.get(key, [])]
        left = [p for p in self.data.rglob("*")] if self.data.is_dir() else []
        old = []
        for folder in (self.data.parent, self.data / "backups"):
            if folder.is_dir():
                for pattern in DB_PATTERNS:
                    for p in folder.glob(pattern):
                        try:
                            if datetime.fromtimestamp(p.stat().st_mtime, UTC) < self.requested:
                                old.append(p)
                        except OSError:
                            old.append(p)
        checks = {
            "inventory_absent": not any(p.exists() for p in listed),
            "data_folder_empty": all(p in markers or p in self.kept or (p.is_dir() and p == self.data / "lab")
                                     or (p.is_dir() and self._keep_dir(p)) for p in left),
            "no_old_database_files": not old,
            "no_lab_database": not any(p.exists() for p in lab_suspension.lab_databases(self.data)),
        }
        self.state["verification"] = checks
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            raise Blocked("verification_failed: " + ", ".join(failed))

    def step_complete(self) -> None:
        lab_suspension.resume(self.data)
        for d in (self.data / "lab", self.data):
            try:
                d.rmdir()
            except OSError:
                pass
        self.state["completed_at"] = _now()
        self.path.unlink(missing_ok=True)
        _fsync_dir(self.path.parent)
        logger.info("Delete all complete for %s.", self.data)

