"""Delete all application data: database-free, resumable procedure (ADR 0015)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.feedback.service import Delivery, FeedbackService
from src.privacy import delete_all
from src.storage import app_state
from src.storage.app_state import GenerationMismatch
from src.storage.sqlite import Storage
from tests.test_privacy_erasure import _seed


def _forget_schema_caches() -> None:
    """A real restart is a new process; in one test process the storages remember schemas they set
    up on the deleted database."""
    import gc
    for obj in gc.get_objects():
        if isinstance(obj, type) and isinstance(getattr(obj, "_initialized_paths", None), set):
            obj._initialized_paths.clear()


@pytest.fixture
def root(tmp_path) -> Path:
    """An install root: data/ (seeded), config/ (a tracked default and a personal profile), logs/."""
    r = tmp_path / "install"
    data = r / "data"
    data.mkdir(parents=True)
    _seed(str(data / "jobs.db"))
    (data / "settings.json").write_text("{}", encoding="utf-8")
    (data / "lab").mkdir()
    (data / "lab" / "lab.db").write_bytes(b"lab")
    (data / "lab.db").write_bytes(b"lab")
    cfg = r / "config"
    cfg.mkdir()
    (cfg / "scoring.yaml").write_text("weights: {}\n", encoding="utf-8")
    (cfg / "profile.yaml").write_text("name: example\n", encoding="utf-8")
    (cfg / "profile.local.yaml").write_text("name: me\n", encoding="utf-8")
    (cfg / "profile.v1-backup.local.yaml").write_text("name: old me\n", encoding="utf-8")
    (r / "logs" / "backend").mkdir(parents=True)
    (r / "logs" / "backend" / "backend-2026-11-05.log").write_text("started\n", encoding="utf-8")
    return r


def _request(root: Path) -> Path:
    data = root / "data"
    delete_all.request(data / "jobs.db", root / "config")
    return data


def _assert_deleted(root: Path) -> None:
    assert not (root / "data").exists()
    assert not delete_all.pending(root / "data")
    assert sorted(p.name for p in (root / "config").iterdir()) == ["profile.yaml", "scoring.yaml"]
    assert not list((root / "logs").rglob("*.log"))


def test_full_procedure_deletes_everything_and_keeps_defaults(root):
    data = _request(root)
    assert (root / delete_all.PENDING).exists()
    st = delete_all.run(data)
    assert st["state"] == "COMPLETE", st
    assert st["verification"] == {"inventory_absent": True, "data_folder_empty": True,
                                  "no_old_database_files": True, "no_lab_database": True}
    assert any(e["path"].endswith("jobs.db") for e in st["inventory"]["data"])
    assert len(st["inventory"]["config"]) == 2 and st["skipped"] == []
    _assert_deleted(root)


@pytest.mark.parametrize("crash_after", delete_all.STATES[1:-1])
def test_resumes_from_the_state_file_alone(root, crash_after):
    data = _request(root)
    st = delete_all.run(data, stop_after=crash_after)
    assert st["state"] == crash_after and delete_all.pending(data)
    if crash_after in ("FILES_DELETED", "VERIFIED"):
        assert not (data / "jobs.db").exists()            # the database is gone, the procedure is not
    st = delete_all.run(data)
    assert st["state"] == "COMPLETE", st and st["attempts"] == 2
    _assert_deleted(root)


def test_locked_file_blocks_then_retry_completes(root, monkeypatch):
    data = _request(root)
    real_unlink = Path.unlink

    def locked(self, missing_ok=False):
        if self.name == "settings.json":
            raise PermissionError(13, "in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", locked)
    st = delete_all.run(data)
    assert st["state"] == "BLOCKED" and st["last_state"] == "INVENTORIED"
    assert "settings.json" in st["last_error"] and st["last_error"].startswith("file_locked")
    assert delete_all.pending(data)
    monkeypatch.undo()
    assert delete_all.run(data)["state"] == "COMPLETE"
    _assert_deleted(root)


def test_lab_suspended_during_the_procedure_and_lab_recreation_blocks(root):
    data = _request(root)
    delete_all.run(data, stop_after="FILES_DELETED")
    assert (data / delete_all.LAB_MARKER).exists() and (data / "lab" / delete_all.LAB_MARKER).exists()
    (data / "lab" / "lab.db").write_bytes(b"recreated")       # a lab writer that ignored the marker
    st = delete_all.run(data)
    assert st["state"] == "BLOCKED" and "no_lab_database" in st["last_error"]


def test_a_log_held_open_is_skipped_not_blocking(root, monkeypatch):
    data = _request(root)
    real_unlink = Path.unlink

    def held(self, missing_ok=False):
        if self.suffix == ".log":
            raise PermissionError(13, "in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", held)
    st = delete_all.run(data)
    assert st["state"] == "COMPLETE" and len(st["skipped"]) == 1


def test_refuses_a_folder_that_is_not_a_data_folder(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(ValueError):
        delete_all.request(tmp_path / "repo" / "jobs.db", tmp_path / "config")


def test_old_epoch_entry_is_rejected_by_the_new_database(root):
    data = root / "data"
    old = app_state.current(str(data / "jobs.db")).token
    assert old.endswith(":0")
    _request(root)
    delete_all.run(data)
    _forget_schema_caches()
    data.mkdir()
    s = Storage(db_path=str(data / "jobs.db"))
    s.init_db()
    s.close()
    new = app_state.current(str(data / "jobs.db")).token
    assert new.endswith(":0") and new != old                 # same generation, another epoch
    from tests.test_feedback_delivery import _job
    s = Storage(db_path=str(data / "jobs.db"))
    s.upsert_job(_job("j1"))
    s.close()
    with pytest.raises(GenerationMismatch):
        FeedbackService(str(data / "jobs.db")).record_list("j1", "saved", delivery=Delivery(generation_token=old))


@pytest.fixture
def app_in_root(isolated_app, root, monkeypatch):
    from src.api.runner import PipelineRunner
    db = str(root / "data" / "jobs.db")
    monkeypatch.setitem(isolated_app.state._state, "db_path", db)
    monkeypatch.setitem(isolated_app.state._state, "config_dir", str(root / "config"))
    monkeypatch.setitem(isolated_app.state._state, "settings_path", str(root / "data" / "settings.json"))
    monkeypatch.setitem(isolated_app.state._state, "runner", PipelineRunner(
        db_path=db, config_dir=str(root / "config"), progress_bus=isolated_app.state.progress_bus))
    for key in ("restart_required", "delete_all_blocked", "delete_all_last", "supervised",
                "delete_all_restarting", "exit_process"):
        monkeypatch.setitem(isolated_app.state._state, key, None)
    monkeypatch.setitem(isolated_app.state._state, "erasure_sync", True)
    return isolated_app


def test_api_delete_all_then_restart(app_in_root, root):
    with TestClient(app_in_root) as client:
        assert client.post("/api/privacy/delete-all", json={}).status_code == 400
        r = client.post("/api/privacy/delete-all", json={"confirm": True})
        assert r.status_code == 202, r.text
        body = r.json()
        if body["state"] == "COMPLETE":
            assert body["restart_required"] is True and body["pending"] is False
            r = client.get("/api/jobs")
            assert r.status_code == 503 and r.json()["detail"] == "restart_required"
        else:
            # Windows: this process still holds the database open; the restart finishes the job
            assert body["state"] == "BLOCKED" and body["last_error"].startswith("file_locked")
            r = client.get("/api/jobs")
            assert r.status_code == 423 and r.json()["detail"] == "delete_all_running"
        # nothing recreates the database until the restart
        assert "X-Erasure-Generation" not in client.get("/api/privacy/delete-all").headers
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/privacy/delete-all", json={"confirm": True}).status_code == 409
    _forget_schema_caches()
    app_in_root.state.restart_required = None
    app_in_root.state.delete_all_blocked = None
    with TestClient(app_in_root) as client:          # the restart
        assert client.get("/api/privacy/delete-all").json()["pending"] is False
        assert client.get("/api/jobs").status_code == 200
    assert sorted(p.name for p in (root / "config").iterdir()) == ["profile.yaml", "scoring.yaml"]


def test_startup_resumes_before_opening_the_database(app_in_root, root):
    data = _request(root)
    delete_all.run(data, stop_after="QUIESCED")
    _forget_schema_caches()
    with TestClient(app_in_root) as client:
        # resumed and completed at startup; then a fresh database is created
        assert client.get("/api/privacy/delete-all").json()["pending"] is False
        assert client.get("/api/session").status_code == 200
    assert (root / "data" / "jobs.db").exists()
    conn = sqlite3.connect(root / "data" / "jobs.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_startup_with_a_blocked_procedure_starts_nothing(app_in_root, root, monkeypatch):
    data = _request(root)
    delete_all.run(data, stop_after="INVENTORIED")
    real_unlink = Path.unlink

    def locked(self, missing_ok=False):
        if self.name == "settings.json":
            raise PermissionError(13, "in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", locked)
    with TestClient(app_in_root) as client:
        r = client.get("/api/jobs")
        assert r.status_code == 423 and r.json()["detail"] == "delete_all_running"
        st = client.get("/api/privacy/delete-all").json()
        assert st["state"] == "BLOCKED" and st["pending"] is True
    assert not (data / "jobs.db").exists()                     # no new database was created


# -- launcher-owned files and the automatic restart (Windows locked files) ------------------------

def _launcher_files(root: Path) -> list[Path]:
    files = [root / "data" / "state" / "instance.json", root / "data" / "state" / "api-port.json",
             root / "logs" / "launcher" / "state.json", root / "logs" / "launcher" / "launcher.log",
             root / "logs" / "launcher" / "backend.log"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    return files


def test_launcher_owned_files_are_never_deleted(root):
    kept = _launcher_files(root)
    (root / "data" / "state" / "other.json").write_text("{}", encoding="utf-8")   # app state: deleted
    data = _request(root)
    st = delete_all.run(data)
    assert st["state"] == "COMPLETE", st
    assert st["verification"]["data_folder_empty"] is True
    assert all(f.exists() for f in kept)
    assert sorted(p.name for p in (root / "data").rglob("*")) == ["api-port.json", "instance.json", "state"]
    assert not (root / "logs" / "backend").exists() or not list((root / "logs" / "backend").iterdir())
    assert sorted(p.name for p in (root / "config").iterdir()) == ["profile.yaml", "scoring.yaml"]
    assert not any(e["path"].endswith(("instance.json", "api-port.json")) for e in st["inventory"]["data"])
    assert not any(Path(e["path"]).is_relative_to(root / "logs" / "launcher") for e in st["inventory"]["logs"])


def test_one_automatic_restart_per_request(root):
    data = root / "data"
    assert delete_all.claim_auto_restart(data) is False                     # nothing pending
    _request(root)
    assert delete_all.claim_auto_restart(data) is True
    assert delete_all.claim_auto_restart(data) is False                     # used: never loops
    assert delete_all.read_state(data)["auto_restarts"] == 1


def _lock_files(monkeypatch) -> None:
    def blocked(self):
        raise delete_all.Blocked("file_locked: jobs.db (in use)")
    monkeypatch.setattr(delete_all._Procedure, "step_files_deleted", blocked)


def _wait_for(cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.05)
    return cond()


def test_supervised_backend_restarts_once_on_a_locked_file(app_in_root, root, monkeypatch):
    from src.api.routes import privacy
    from src.launcher.core import RESTART_EXIT_CODE

    _lock_files(monkeypatch)
    monkeypatch.setattr(privacy, "RESTART_DELAY_S", 0.0)
    exits: list[int] = []
    app_in_root.state.supervised = True
    app_in_root.state.exit_process = exits.append
    with TestClient(app_in_root) as client:
        body = client.post("/api/privacy/delete-all", json={"confirm": True}).json()
        assert body["state"] == "BLOCKED" and body["restarting"] is True and body["auto_restarts"] == 1
        assert _wait_for(lambda: exits == [RESTART_EXIT_CODE])
        # the restart failed to release the file: the retry reports it and never restarts again
        app_in_root.state.delete_all_restarting = False
        body = client.post("/api/privacy/delete-all/retry").json()
        assert body["state"] == "BLOCKED" and body["restarting"] is False and body["pending"] is True
    assert exits == [RESTART_EXIT_CODE]
    assert delete_all.pending(root / "data")                                 # still pending: recoverable


def test_unsupervised_backend_never_exits(app_in_root, root, monkeypatch):
    _lock_files(monkeypatch)
    exits: list[int] = []
    app_in_root.state.exit_process = exits.append
    with TestClient(app_in_root) as client:
        body = client.post("/api/privacy/delete-all", json={"confirm": True}).json()
        assert body["state"] == "BLOCKED" and body["restarting"] is False
    assert exits == [] and delete_all.read_state(root / "data")["auto_restarts"] == 0


def test_completion_at_startup_is_reported_after_the_restart(app_in_root, root):
    data = _request(root)
    delete_all.run(data, stop_after="QUIESCED")
    _forget_schema_caches()
    with TestClient(app_in_root) as client:
        st = client.get("/api/privacy/delete-all").json()
        assert st["state"] == "COMPLETE" and st["pending"] is False and st["completed_at"]
        assert st["restart_required"] is False and st["restarting"] is False
