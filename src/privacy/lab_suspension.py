"""Lab writers' suspension during a lab purge or "Delete all" (ADR 0015, v5 lab purge rules 3–6).

A content-free ``LAB_SUSPENDED`` marker in ``data/`` and in ``data/lab/`` suspends every lab writer:
the one function that opens a lab database (``src.lab.store.LabStore``) refuses to open, and refuses
to create, a lab database while a marker sits beside it or in its parent folder. The markers stay until
the procedure's verification has re-checked that no lab file reappeared.

Dependency-free on purpose: production code must not import ``src.lab`` (ADR 0011), and the lab may
import this module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

LAB_MARKER = "LAB_SUSPENDED"
LAB_DB = "lab.db"


class LabSuspended(RuntimeError):
    """A lab database was opened while a purge or "Delete all" suspends the lab writers."""


def markers(data_dir: str | Path) -> list[Path]:
    data = Path(data_dir)
    return [data / LAB_MARKER, data / "lab" / LAB_MARKER]


def lab_databases(data_dir: str | Path) -> list[Path]:
    """Both lab database locations (the API reads one of them; a purge must cover both)."""
    data = Path(data_dir)
    return [data / LAB_DB, data / "lab" / LAB_DB]


def suspend(data_dir: str | Path) -> None:
    """Write the markers (in ``data/lab/`` only when that folder exists)."""
    for marker in markers(data_dir):
        if marker.parent.is_dir():
            marker.write_text("", encoding="utf-8")


def resume(data_dir: str | Path) -> None:
    for marker in markers(data_dir):
        marker.unlink(missing_ok=True)


def is_suspended(lab_db: str | Path) -> bool:
    """True when a marker sits beside ``lab_db`` or in its parent folder."""
    folder = Path(lab_db).resolve().parent
    return (folder / LAB_MARKER).exists() or (folder.parent / LAB_MARKER).exists()


def check_can_open(lab_db: str | Path) -> None:
    if is_suspended(lab_db):
        raise LabSuspended(f"the lab is suspended while application data is being erased ({lab_db})")


def prove_no_writer(lab_db: Path, timeout: float = 2.0) -> None:
    """Take and release an exclusive lock: raises ``sqlite3.OperationalError`` while another
    connection holds the database."""
    conn = sqlite3.connect(str(lab_db), timeout=timeout, isolation_level=None)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    finally:
        conn.close()
