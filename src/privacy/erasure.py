"""Erase my activity: a resumable, verified job (ADR 0015, v5 §4.12).

State machine, every step idempotent::

    REQUESTED -> QUIESCED -> INVENTORIED -> DB_ERASED -> WAL_TRUNCATED -> COPIES_REMOVED
              -> VERIFIED -> COMPLETE                      (or BLOCKED, with the reason)

* The sidecar ``ERASURE_PENDING`` marker (beside the database) is written and fsynced **before**
  anything is touched and removed only at COMPLETE. While it exists, the API answers 423 to writes,
  the scheduler stays paused and ``backup_database`` refuses. At startup it is checked before
  migrations, and a pending erasure resumes from its last completed step.
* ``erasure_jobs`` (a control table) holds the state, counts, paths and the verification result,
  never content. The sample of journal ids that verification searches for in the database bytes
  lives in the marker file, outside the database, so the search cannot match itself.
* DB_ERASED is one transaction: append-only triggers dropped, personal and derived rows deleted,
  personal columns set to NULL, triggers recreated, ``erasure_generation`` incremented.
* BLOCKED keeps the last completed step in ``last_state``; ``run`` resumes from there.
* Lab purge (optional, ``Scope.purge_lab``, v5 lab rules 1–7): allowed only while production runs the
  shipped default strategy; the lab writers are suspended at QUIESCED (``LAB_SUSPENDED`` markers), each
  ``lab.db`` must give an exclusive lock before both are deleted as files with ``data/lab/`` at
  COPIES_REMOVED, VERIFIED re-checks that nothing reappeared, the markers go at COMPLETE.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.privacy import lab_suspension
from src.storage import app_state
from src.storage.data_classes import JOBS_DB_TABLES, PERSONAL_COLUMNS

logger = logging.getLogger(__name__)

MARKER = "ERASURE_PENDING"
STATES = ("REQUESTED", "QUIESCED", "INVENTORIED", "DB_ERASED", "WAL_TRUNCATED", "COPIES_REMOVED",
          "VERIFIED", "COMPLETE")
BLOCKED = "BLOCKED"
SAMPLE_SIZE = 50
CHECKPOINT_ATTEMPTS = 10
CALIBRATION_DIR = "calibration"
TRACKER_COLUMN = ("jobs", "application_status")

_DDL = """
    CREATE TABLE IF NOT EXISTS erasure_jobs (
        erasure_id TEXT PRIMARY KEY,
        requested_at TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        state TEXT NOT NULL,
        last_state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        inventory_json TEXT,
        counts_json TEXT,
        generation_before INTEGER,
        generation_after INTEGER,
        verification_json TEXT,
        completed_at TEXT
    )
"""


class Blocked(Exception):
    """A step cannot finish now (locked file, busy checkpoint, failed check); the job waits for a retry."""


class _StopAfter(Exception):
    """Test hook: simulate a crash right after a state was recorded."""


@dataclass(frozen=True)
class Scope:
    keep_tracker: bool = False        # "Conserver mon suivi de candidatures"
    erase_calibration: bool = True    # calibration labels go with the activity (ticked by default)
    purge_lab: bool = False           # "Effacer aussi les données du laboratoire" (separately confirmed)


LAB_STRATEGY_ACTIVE = ("lab_strategy_active: désactivez d'abord la stratégie de laboratoire active "
                       "(deactivate the active lab strategy first)")
LAB_BUSY = ("lab_busy: un processus du laboratoire est actif ; arrêtez-le puis réessayez "
            "(a lab process is running; stop it and retry)")


def lab_present(db_path: str | Path) -> bool:
    """A lab database or lab folder exists in either location (the dialog then offers the purge)."""
    data = Path(db_path).parent
    return any(p.exists() for p in lab_suspension.lab_databases(data)) or (data / "lab").is_dir()


def production_on_default_strategy(db_path: str | Path) -> bool:
    """True when every pointer history (``jobs.db`` and each existing ``lab.db``) is empty or ends with
    ``deactivate``; False when another strategy is active or a history cannot be read."""
    from src.decisions.active_strategy import read_pointer_history
    data = Path(db_path).parent
    for db in [Path(db_path)] + [p for p in lab_suspension.lab_databases(data) if p.exists()]:
        try:
            rows, _ = read_pointer_history(db)
        except sqlite3.Error:
            return False
        if rows and rows[0].get("action") != "deactivate":
            return False
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def marker_path(db_path: str | Path) -> Path:
    return Path(db_path).parent / MARKER


def pending(db_path: str | Path) -> bool:
    """True while an erasure is requested and not COMPLETE (the marker exists)."""
    return marker_path(db_path).exists()


def _write_marker(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_marker(db_path: str | Path) -> dict[str, Any]:
    return json.loads(marker_path(db_path).read_text(encoding="utf-8"))


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    conn.execute(_DDL)
    return conn


def scope_tables() -> list[str]:
    """jobs.db tables an erasure empties: every personal and derived table."""
    return sorted(t for t, c in JOBS_DB_TABLES.items() if c in ("personal", "derived"))


def scope_columns(scope: Scope) -> list[tuple[str, str]]:
    cols = [(t, c) for t, names in PERSONAL_COLUMNS.items() for c in names]
    return [tc for tc in cols if not (scope.keep_tracker and tc == TRACKER_COLUMN)]


def request(db_path: str | Path, scope: Scope, now: str | None = None) -> str:
    """Write the marker (fsynced), then record the job as REQUESTED → its id. A second request while
    one is pending returns the pending id."""
    marker = marker_path(db_path)
    if marker.exists():
        return str(_read_marker(db_path)["erasure_id"])
    erasure_id = str(uuid.uuid4())
    requested_at = now or _now()
    _write_marker(marker, {"erasure_id": erasure_id, "requested_at": requested_at, "scope": asdict(scope)})
    _ensure_row(db_path, erasure_id, requested_at, scope)
    logger.info("Erasure %s requested (scope %s).", erasure_id, asdict(scope))
    return erasure_id


def _ensure_row(db_path: str | Path, erasure_id: str, requested_at: str, scope: Scope) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO erasure_jobs (erasure_id, requested_at, scope_json, state, last_state)"
            " VALUES (?, ?, ?, 'REQUESTED', 'REQUESTED')",
            (erasure_id, requested_at, json.dumps(asdict(scope))))
    finally:
        conn.close()


def status(db_path: str | Path) -> dict[str, Any] | None:
    """The latest erasure job (pending first), as stored: no content, counts and checks only."""
    if not Path(db_path).exists():
        return None
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = None
        if pending(db_path):
            try:
                row = conn.execute("SELECT * FROM erasure_jobs WHERE erasure_id = ?",
                                   (_read_marker(db_path)["erasure_id"],)).fetchone()
            except (OSError, ValueError, KeyError):
                row = None
        if row is None:
            row = conn.execute("SELECT * FROM erasure_jobs ORDER BY requested_at DESC, rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = dict(row)
    for key in ("scope_json", "inventory_json", "counts_json", "verification_json"):
        out[key.removesuffix("_json")] = json.loads(out.pop(key)) if out.get(key) else None
    out["pending"] = pending(db_path)
    return out


# -- running ------------------------------------------------------------------------------------

def run(db_path: str | Path, quiesce: Callable[[], None] | None = None,
        stop_after: str | None = None) -> dict[str, Any] | None:
    """Run (or resume) the pending erasure up to COMPLETE or BLOCKED → its status. ``quiesce`` stops
    the writers (scheduler, pipeline) and returns once they are idle; it raises ``Blocked`` if they
    cannot be stopped. ``stop_after`` (tests) aborts right after that state is recorded."""
    if not pending(db_path):
        return status(db_path)
    marker = _read_marker(db_path)
    erasure_id = str(marker["erasure_id"])
    scope = Scope(**marker.get("scope", {}))
    _ensure_row(db_path, erasure_id, marker["requested_at"], scope)
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE erasure_jobs SET attempts = attempts + 1, state = last_state, last_error = NULL"
                     " WHERE erasure_id = ?", (erasure_id,))
        state = conn.execute("SELECT last_state FROM erasure_jobs WHERE erasure_id = ?",
                             (erasure_id,)).fetchone()[0]
        job = _Job(Path(db_path), conn, erasure_id, marker, scope, quiesce)
        try:
            for nxt in STATES[STATES.index(state) + 1:]:
                getattr(job, "step_" + nxt.lower())()
                if nxt != "DB_ERASED":     # DB_ERASED records its state inside its own transaction
                    job.set_state(nxt)
                if stop_after == nxt:
                    raise _StopAfter(nxt)
        except Blocked as exc:
            conn.execute("UPDATE erasure_jobs SET state = ?, last_error = ? WHERE erasure_id = ?",
                         (BLOCKED, str(exc), erasure_id))
            logger.warning("Erasure %s blocked: %s", erasure_id, exc)
        except _StopAfter:
            pass
    finally:
        conn.close()
    return status(db_path)


class _Job:
    def __init__(self, db_path: Path, conn: sqlite3.Connection, erasure_id: str, marker: dict[str, Any],
                 scope: Scope, quiesce: Callable[[], None] | None) -> None:
        self.db_path = db_path
        self.conn = conn
        self.erasure_id = erasure_id
        self.marker = marker
        self.scope = scope
        self.quiesce = quiesce
        self.requested_at = str(marker["requested_at"])

    def set_state(self, state: str) -> None:
        self.conn.execute("UPDATE erasure_jobs SET state = ?, last_state = ? WHERE erasure_id = ?",
                          (state, state, self.erasure_id))

    def _tables(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    # -- steps --

    def step_quiesced(self) -> None:
        if self.scope.purge_lab:
            if not production_on_default_strategy(self.db_path):
                raise Blocked(LAB_STRATEGY_ACTIVE)
            lab_suspension.suspend(self.db_path.parent)
        if self.quiesce is not None:
            self.quiesce()

    def _lab_files(self) -> list[Path]:
        if not self.scope.purge_lab:
            return []
        data = self.db_path.parent
        markers = set(lab_suspension.markers(data))
        files = [p for db in lab_suspension.lab_databases(data)
                 for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")) if p.exists()]
        lab_dir = data / "lab"
        if lab_dir.is_dir():
            files += [p for p in lab_dir.rglob("*") if p.is_file() and p not in markers and p not in files]
        return sorted(set(files))

    def step_inventoried(self) -> None:
        tables = self._tables()
        counts = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
                  for t in scope_tables() if t in tables}
        for table, col in scope_columns(self.scope):
            if table in tables and col in self._columns(table):
                counts[f"{table}.{col}"] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL").fetchone()[0]  # noqa: S608
        sample: list[str] = []
        if "feedback_events" in tables:
            sample = [r[0] for r in self.conn.execute(
                "SELECT event_id FROM feedback_events ORDER BY random() LIMIT ?", (SAMPLE_SIZE,))]
        inventory = {"counts": counts, "backups": [p.name for p in self._old_backups()],
                     "calibration": self._calibration_dir().exists() and self.scope.erase_calibration,
                     "lab_files": len(self._lab_files()), "sampled_ids": len(sample)}
        # The sample stays outside the database (marker file), so the byte search cannot find itself.
        self.marker["sample"] = sample
        _write_marker(marker_path(self.db_path), self.marker)
        self.conn.execute("UPDATE erasure_jobs SET inventory_json = ? WHERE erasure_id = ?",
                          (json.dumps(inventory), self.erasure_id))

    def step_db_erased(self) -> None:
        conn = self.conn
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = self._tables()
            in_scope = [t for t in scope_tables() if t in tables]
            triggers = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL AND tbl_name IN"
                f" ({','.join('?' * len(in_scope))})", in_scope).fetchall() if in_scope else []
            for name, _ in triggers:
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            counts: dict[str, int] = {}
            for table in in_scope:
                counts[table] = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608 — fixed names
            for table, col in scope_columns(self.scope):
                if table in tables and col in self._columns(table):
                    counts[f"{table}.{col}"] = conn.execute(
                        f"UPDATE {table} SET {col} = NULL WHERE {col} IS NOT NULL").rowcount  # noqa: S608
            for _, sql in triggers:
                conn.execute(sql)
            before = app_state.read(conn).erasure_generation
            after = app_state.increment_generation(conn)
            conn.execute(
                "UPDATE erasure_jobs SET state = 'DB_ERASED', last_state = 'DB_ERASED', counts_json = ?,"
                " generation_before = ?, generation_after = ? WHERE erasure_id = ?",
                (json.dumps(counts), before, after, self.erasure_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _wal(self) -> Path:
        return self.db_path.with_name(self.db_path.name + "-wal")

    def step_wal_truncated(self) -> None:
        try:
            size = self.db_path.stat().st_size
            if shutil.disk_usage(self.db_path.parent).free > size * 2:
                self.conn.execute("VACUUM")
        except (OSError, sqlite3.Error) as exc:
            logger.info("Erasure %s: VACUUM skipped (%s).", self.erasure_id, exc)
        self._checkpoint()

    def _checkpoint(self) -> None:
        for attempt in range(CHECKPOINT_ATTEMPTS):
            busy = 0
            try:
                busy = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            except sqlite3.OperationalError:
                busy = 1
            if busy == 0 and self._wal_size() == 0:
                return
            time.sleep(0.2 * (attempt + 1))
        raise Blocked("checkpoint_busy: another program keeps the database open; close it and retry")

    def _wal_size(self) -> int:
        try:
            return self._wal().stat().st_size
        except FileNotFoundError:
            return 0

    def _calibration_dir(self) -> Path:
        return self.db_path.parent / CALIBRATION_DIR

    def _old_backups(self) -> list[Path]:
        folder = self.db_path.parent / "backups"
        if not folder.is_dir():
            return []
        cutoff = _parse(self.requested_at)
        out = []
        for path in sorted(folder.glob(f"{self.db_path.stem}-*.db")):
            when = _backup_time(path)
            if when is None or when <= cutoff:
                out.append(path)
        return out

    def step_copies_removed(self) -> None:
        for path in self._old_backups():
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise Blocked(f"file_locked: {path.name} ({exc.strerror or exc})") from exc
        cal = self._calibration_dir()
        if self.scope.erase_calibration and cal.exists():
            try:
                shutil.rmtree(cal)
            except OSError as exc:
                raise Blocked(f"file_locked: {CALIBRATION_DIR} ({exc.strerror or exc})") from exc
        if self.scope.purge_lab:
            self._purge_lab()

    def _purge_lab(self) -> None:
        data = self.db_path.parent
        for db in lab_suspension.lab_databases(data):
            if db.exists():
                try:
                    lab_suspension.prove_no_writer(db)
                except sqlite3.Error as exc:
                    raise Blocked(LAB_BUSY) from exc
        for p in self._lab_files():
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                if p.name.startswith("lab.db"):
                    raise Blocked(LAB_BUSY) from exc
                raise Blocked(f"file_locked: {p.relative_to(data)} ({exc.strerror or exc})") from exc
        lab_dir = data / "lab"
        if lab_dir.is_dir():
            for d in sorted((d for d in lab_dir.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass

    def step_verified(self) -> None:
        # the state updates since WAL_TRUNCATED went to the WAL (control rows only): flush them first
        self._checkpoint()
        checks = verify(self.db_path, self.conn, self.erasure_id, self.scope,
                        [str(s) for s in self.marker.get("sample", [])], self._old_backups(),
                        self._calibration_dir(), self._wal_size())
        if self.scope.purge_lab:
            checks["lab"] = not self._lab_files()
        self.conn.execute("UPDATE erasure_jobs SET verification_json = ? WHERE erasure_id = ?",
                          (json.dumps(checks), self.erasure_id))
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise Blocked("verification_failed: " + ", ".join(failed))

    def step_complete(self) -> None:
        if self.scope.purge_lab:
            lab_suspension.resume(self.db_path.parent)
            try:
                (self.db_path.parent / "lab").rmdir()     # empty once purged
            except OSError:
                pass
        self.conn.execute("UPDATE erasure_jobs SET completed_at = ? WHERE erasure_id = ?",
                          (_now(), self.erasure_id))
        marker_path(self.db_path).unlink(missing_ok=True)
        logger.info("Erasure %s complete.", self.erasure_id)


def verify(db_path: Path, conn: sqlite3.Connection, erasure_id: str, scope: Scope, sample: list[str],
           old_backups: list[Path], calibration: Path, wal_size: int) -> dict[str, bool]:
    """The five checks of ADR 0015: rows, WAL, bytes, copies, generation."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    rows_ok = all(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0  # noqa: S608
                  for t in scope_tables() if t in tables)
    for table, col in scope_columns(scope):
        if table in tables and col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            rows_ok = rows_ok and conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL").fetchone()[0] == 0  # noqa: S608
    data = db_path.read_bytes()
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    bytes_ok = not any(s.encode("utf-8") in data for s in sample if s)
    copies_ok = not old_backups and not (scope.erase_calibration and calibration.exists())
    gen_ok = False
    row = conn.execute("SELECT generation_before, generation_after FROM erasure_jobs WHERE erasure_id = ?",
                       (erasure_id,)).fetchone()
    if row and row[0] is not None:
        state = app_state.read(conn)
        gen_ok = state.erasure_generation == row[1]
        # probe: a write stamped with the pre-erasure generation is refused (and nothing is written)
        conn.execute("BEGIN")
        try:
            app_state.check_token(conn, f"{state.data_epoch}:{row[0]}")
            gen_ok = False
        except app_state.GenerationMismatch:
            pass
        finally:
            conn.execute("ROLLBACK")
    return {"rows": rows_ok, "wal": wal_size == 0, "bytes": bytes_ok, "copies": copies_ok,
            "generation": gen_ok}


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _backup_time(path: Path) -> datetime | None:
    stamp = path.stem.rsplit("-", 1)[-1]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
