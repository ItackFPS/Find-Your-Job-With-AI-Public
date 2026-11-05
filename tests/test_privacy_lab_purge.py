"""L1.8: lab writers suspended during a purge, optional lab purge in "Erase my activity" (ADR 0015)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.lab.store import LabStore
from src.privacy import erasure, lab_suspension
from tests.test_privacy_erasure import _seed


def _pointer(db: Path, action: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS active_strategy (pointer_id TEXT PRIMARY KEY, set_at TEXT NOT NULL,"
                     " bundle_hash TEXT NOT NULL, bundle_path TEXT NOT NULL, approved_by TEXT NOT NULL,"
                     " candidate_id TEXT, experiment_id TEXT, reason TEXT NOT NULL, action TEXT NOT NULL,"
                     " pipeline_effective INTEGER NOT NULL DEFAULT 0)")
        n = conn.execute("SELECT COUNT(*) FROM active_strategy").fetchone()[0]
        conn.execute("INSERT INTO active_strategy VALUES (?, ?, 'h', 'b', 'owner', NULL, NULL, 'r', ?, 1)",
                     (f"p{n}", f"2026-11-0{n + 1}T00:00:00+00:00", action))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "jobs.db")
    _seed(path)
    return path


def _make_lab(data: Path) -> None:
    LabStore(data / "lab.db").close()
    (data / "lab").mkdir(exist_ok=True)
    LabStore(data / "lab" / "lab.db").close()
    (data / "lab" / "reports").mkdir()
    (data / "lab" / "reports" / "r1.md").write_text("observation", encoding="utf-8")


def test_lab_store_refuses_to_open_or_create_while_suspended(tmp_path):
    LabStore(tmp_path / "lab.db").close()
    lab_suspension.suspend(tmp_path)
    with pytest.raises(lab_suspension.LabSuspended):
        LabStore(tmp_path / "lab.db")
    (tmp_path / "lab").mkdir()
    with pytest.raises(lab_suspension.LabSuspended):        # marker in the parent folder covers data/lab/
        LabStore(tmp_path / "lab" / "lab.db")
    assert not (tmp_path / "lab" / "lab.db").exists()
    lab_suspension.resume(tmp_path)
    LabStore(tmp_path / "lab.db").close()


def test_lab_present_and_default_strategy(db):
    data = Path(db).parent
    assert not erasure.lab_present(db)
    assert erasure.production_on_default_strategy(db)
    _make_lab(data)
    assert erasure.lab_present(db)
    _pointer(Path(db), "approve")
    assert not erasure.production_on_default_strategy(db)
    _pointer(Path(db), "deactivate")
    assert erasure.production_on_default_strategy(db)
    _pointer(data / "lab.db", "approve")                    # a lab-side pointer counts too
    assert not erasure.production_on_default_strategy(db)


def test_active_strategy_blocks_the_purge_before_anything_is_erased(db):
    data = Path(db).parent
    _make_lab(data)
    _pointer(Path(db), "approve")
    erasure.request(db, erasure.Scope(purge_lab=True))
    st = erasure.run(db)
    assert st["state"] == "BLOCKED" and st["last_state"] == "REQUESTED"
    assert st["last_error"].startswith("lab_strategy_active")
    assert (data / "lab.db").exists() and not (data / lab_suspension.LAB_MARKER).exists()


def test_purge_deletes_both_lab_databases_and_verifies(db):
    data = Path(db).parent
    _make_lab(data)
    erasure.request(db, erasure.Scope(purge_lab=True))
    st = erasure.run(db)
    assert st["state"] == "COMPLETE", st
    assert st["verification"]["lab"] is True
    assert st["inventory"]["lab_files"] >= 3
    assert not any(p.exists() for p in lab_suspension.lab_databases(data))
    assert not list((data / "lab").rglob("*")) if (data / "lab").exists() else True
    assert not any(m.exists() for m in lab_suspension.markers(data))
    LabStore(data / "lab.db").close()                  # the lab works again afterwards


def test_purge_off_by_default_keeps_the_lab(db):
    data = Path(db).parent
    _make_lab(data)
    erasure.request(db, erasure.Scope())
    assert erasure.run(db)["state"] == "COMPLETE"
    assert (data / "lab.db").exists() and (data / "lab" / "reports" / "r1.md").exists()


def test_held_lab_database_blocks_then_retry_finishes(db):
    data = Path(db).parent
    _make_lab(data)
    holder = sqlite3.connect(data / "lab.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE t (x)")
    try:
        erasure.request(db, erasure.Scope(purge_lab=True))
        st = erasure.run(db)
        assert st["state"] == "BLOCKED" and st["last_error"].startswith("lab_busy"), st
        assert (data / lab_suspension.LAB_MARKER).exists()      # still suspended while blocked
        with pytest.raises(lab_suspension.LabSuspended):
            LabStore(data / "lab.db")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    st = erasure.run(db)
    assert st["state"] == "COMPLETE" and st["verification"]["lab"] is True


def test_api_purge_needs_its_own_confirmation(isolated_app):
    with TestClient(isolated_app) as client:
        db = isolated_app.state.db_path
        _seed(db)
        _make_lab(Path(db).parent)
        isolated_app.state.erasure_sync = True
        assert client.get("/api/privacy/erase").json()["lab_present"] is True
        r = client.post("/api/privacy/erase", json={"confirm": True, "purge_lab": True})
        assert r.status_code == 400
        r = client.post("/api/privacy/erase", json={"confirm": True, "purge_lab": True, "confirm_lab": True})
        assert r.status_code == 202 and r.json()["state"] == "COMPLETE", r.json()
        assert client.get("/api/privacy/erase").json()["lab_present"] is False
