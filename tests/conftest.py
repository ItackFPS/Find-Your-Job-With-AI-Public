"""Shared pytest fixtures/guards for the whole test suite."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import traceback
from pathlib import Path

import pytest

# Starlette's TestClient sends "Host: testserver"; the app serves only
# loopback hosts plus API_ALLOWED_HOSTS (issue #59). Set before any test
# module imports src.api.app, which reads it once.
os.environ["API_ALLOWED_HOSTS"] = ",".join(
    filter(None, [os.environ.get("API_ALLOWED_HOSTS", ""), "testserver"])
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_CONFIG_DIR = _REPO_ROOT / "config"
_REAL_DATA_DIR = _REPO_ROOT / "data"


def _sqlite_target_path(database: object) -> Path | None:
    """Filesystem path of a ``sqlite3.connect`` target (str, bytes, PathLike or ``file:`` URI)."""
    if isinstance(database, os.PathLike):
        database = os.fspath(database)
    if isinstance(database, bytes):
        database = os.fsdecode(database)
    if not isinstance(database, str) or database in ("", ":memory:"):
        return None
    if database.startswith("file:"):
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(database)
        if "mode=memory" in parts.query:
            return None
        database = unquote(parts.path)
        if os.name == "nt" and database.startswith("/") and database[2:3] == ":":
            database = database[1:]
    try:
        return Path(os.path.abspath(database)).resolve()
    except (OSError, ValueError):
        return None


def _refuse_real_database(event: str, args: tuple) -> None:
    """Audit hook: no test may open ANY SQLite database under the real data/ directory — not even
    read-only (``Storage.init_db`` alone would migrate the user's database). Raises before connecting."""
    if event != "sqlite3.connect" or not args:
        return
    path = _sqlite_target_path(args[0])
    if path is not None and (path == _REAL_DATA_DIR or _REAL_DATA_DIR in path.parents):
        raise PermissionError(f"test tried to open the real database {path}; use a tmp_path database")


sys.addaudithook(_refuse_real_database)   # audit hooks cannot be removed: active for the whole session


def _snapshot_data_dir() -> dict[str, tuple[int, float]]:
    """(size, mtime) per file directly under the real data/ directory.

    Deliberately NOT a content hash — data/jobs.db is multi-GB, so hashing
    it on every test would be far too slow. (size, mtime) is enough to
    detect a write or a new file appearing (e.g. data/esco.db-wal/-shm).
    """
    snap: dict[str, tuple[int, float]] = {}
    if not _REAL_DATA_DIR.is_dir():
        return snap
    for path in _REAL_DATA_DIR.iterdir():
        if path.is_file():
            st = path.stat()
            snap[path.name] = (st.st_size, st.st_mtime)
    return snap


def _live_threads_report() -> str:
    """Names and stacks of every live thread except the current one.

    A leaked write often comes from a background thread started by an
    EARLIER test (runner, worker, scheduler dispatch) rather than from the
    test the guard fails, so the offender is usually visible here.
    """
    frames = sys._current_frames()
    current = threading.get_ident()
    parts = []
    for thread in threading.enumerate():
        if thread.ident == current:
            continue
        frame = frames.get(thread.ident) if thread.ident is not None else None
        stack = "".join(traceback.format_stack(frame, limit=8)) if frame is not None else ""
        parts.append(f"--- {thread.name} (daemon={thread.daemon})\n{stack}")
    return "Live threads:\n" + ("\n".join(parts) if parts else "(none)")


@pytest.fixture(autouse=True)
def _guard_real_data_dir_untouched():
    before = _snapshot_data_dir()
    yield
    after = _snapshot_data_dir()
    if after != before:
        changed = sorted(
            name for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        )
        pytest.fail(
            "Test wrote to the real data/ directory instead of an isolated "
            f"tmp_path: {changed}. If this test uses `with TestClient(app)` "
            "(which runs the real FastAPI lifespan), override "
            "app.state.db_path/config_dir/esco_db_path to tmp paths first — "
            "see tests/test_esco.py for the pattern.\n"
            "The guard cannot tell writers apart: if several unrelated tests fail "
            "this way, another PROCESS is probably writing data/ (the backend "
            "or its scheduler running from this checkout): stop it and re-run.\n\n"
            + _live_threads_report()
        )


def _hash_config_dir() -> dict[str, str]:
    """Content hash of every file directly under the real config/ directory.

    Tests must never write to the real config/ directory (CLAUDE.md: "Never
    modify/reset real data") — several NotificationEngine/discord_admin
    entry points default `config_dir` to the real "config" string when a
    test forgets to override it, which previously corrupted
    config/notifications.yaml with a MagicMock repr during a full test run.
    This guard fails loudly, close to the offending test, instead of
    silently leaving the repo's committed config mutated.
    """
    hashes: dict[str, str] = {}
    if not _REAL_CONFIG_DIR.is_dir():
        return hashes
    for path in _REAL_CONFIG_DIR.iterdir():
        if path.is_file():
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


@pytest.fixture(autouse=True)
def _guard_real_config_dir_untouched():
    before = _hash_config_dir()
    yield
    after = _hash_config_dir()
    if after != before:
        changed = sorted(
            name for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        )
        report = _live_threads_report()
        # A file the test CREATED (e.g. config/notifications.local.yaml) is
        # removed: left behind, a personal *.local.yaml override would
        # silently replace the developer's real configuration at runtime.
        # Files that existed before the test are never touched.
        created = sorted(set(after) - set(before))
        for name in created:
            (_REAL_CONFIG_DIR / name).unlink(missing_ok=True)
        pytest.fail(
            "Test wrote to the real config/ directory instead of an "
            f"isolated tmp_path: {changed}. Pass an explicit config_dir "
            "(e.g. NotificationEngine(config_dir=str(tmp_path))). "
            f"Removed newly created files: {created}.\n\n" + report
        )


@pytest.fixture(autouse=True)
def _restore_environment():
    """Undo environment changes a test makes without monkeypatch.

    src.discord_admin.secrets.store_webhook_secret sets os.environ directly;
    a webhook URL left behind enables Discord providers in every later test
    (issue #46).
    """
    before = dict(os.environ)
    yield
    # Key by key rather than clear()+update(): the environment is never
    # empty, even briefly, for a daemon thread still running.
    for key in set(os.environ) - set(before):
        os.environ.pop(key, None)
    for key, value in before.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """The FastAPI app with everything its lifespan uses pointed at tmp paths.

    `with TestClient(app)` runs the real lifespan: scheduler, Discord
    reconciler and system-status workers. Overriding app.state's paths is not
    enough — app.state.notification_engine and app.state.runner are built at
    import time against the real config/ and data/ (issue #46).
    """
    from src.api.app import app
    from src.api.runner import PipelineRunner
    from src.notifications.engine import NotificationEngine

    db_path = str(tmp_path / "app_test.db")
    config_dir = str(tmp_path / "empty_config")
    engine = NotificationEngine(db_path=db_path, config_dir=config_dir)
    monkeypatch.setattr(app.state, "db_path", db_path)
    monkeypatch.setattr(app.state, "config_dir", config_dir)
    monkeypatch.setattr(app.state, "esco_db_path", str(tmp_path / "esco_test.db"))
    monkeypatch.setattr(app.state, "notification_engine", engine)
    monkeypatch.setattr(app.state, "runner", PipelineRunner(
        db_path=db_path, config_dir=config_dir,
        notification_engine=engine, progress_bus=app.state.progress_bus,
    ))
    return app
