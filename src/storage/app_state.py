"""The one-row ``app_state`` control table: data epoch, erasure generation, learning switches (ADR 0015).

* ``data_epoch``: a random UUID written when the database is created. An entry stamped by a deleted
  database carries another epoch and is rejected, whatever its generation.
* ``erasure_generation``: starts at 0, incremented only inside an erasure's DB_ERASED transaction,
  never set by a client.
* ``learning_paused`` / ``learning_reset_at``: Pause and Reset learning (ADR 0015 four actions).

Every personal write carries the token ``<data_epoch>:<generation>`` the client held when the user
acted; ``check_token`` compares it with the current value inside the writer's transaction.

The table holds no personal content; it is a **control** table (``src/storage/data_classes.py``).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

HEADER = "X-Erasure-Generation"

_DDL = """
    CREATE TABLE IF NOT EXISTS app_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        data_epoch TEXT NOT NULL,
        erasure_generation INTEGER NOT NULL DEFAULT 0,
        learning_paused INTEGER NOT NULL DEFAULT 0,
        learning_reset_at TEXT,
        updated_at TEXT NOT NULL
    )
"""


class GenerationMismatch(Exception):
    """A personal write stamped with another epoch or generation (HTTP 409)."""

    code = "erasure_generation_mismatch"


@dataclass(frozen=True)
class AppState:
    data_epoch: str
    erasure_generation: int
    learning_paused: bool
    learning_reset_at: str | None

    @property
    def token(self) -> str:
        return f"{self.data_epoch}:{self.erasure_generation}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure(conn: sqlite3.Connection) -> None:
    """Create the table and its single row (with a fresh random epoch) when missing."""
    conn.execute(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO app_state (id, data_epoch, erasure_generation, updated_at) VALUES (1, ?, 0, ?)",
        (str(uuid.uuid4()), _now()),
    )


def read(conn: sqlite3.Connection) -> AppState:
    ensure(conn)
    row = conn.execute(
        "SELECT data_epoch, erasure_generation, learning_paused, learning_reset_at FROM app_state WHERE id = 1"
    ).fetchone()
    return AppState(str(row[0]), int(row[1]), bool(row[2]), row[3])


def parse_token(token: str) -> tuple[str, int]:
    epoch, sep, gen = token.rpartition(":")
    if not sep or not epoch or not gen.isdigit():
        raise GenerationMismatch(f"malformed generation token {token!r}")
    return epoch, int(gen)


def check_token(conn: sqlite3.Connection, token: str) -> AppState:
    """Raise ``GenerationMismatch`` unless ``token`` names the current epoch and generation. Call it
    inside the write transaction, before the insert."""
    state = read(conn)
    epoch, gen = parse_token(token)
    if epoch != state.data_epoch or gen != state.erasure_generation:
        raise GenerationMismatch("the activity this entry belongs to was erased")
    return state


def increment_generation(conn: sqlite3.Connection) -> int:
    """Next generation; the caller runs this inside the DB_ERASED transaction."""
    ensure(conn)
    conn.execute("UPDATE app_state SET erasure_generation = erasure_generation + 1, updated_at = ? WHERE id = 1",
                 (_now(),))
    return read(conn).erasure_generation


def set_learning_paused(conn: sqlite3.Connection, paused: bool) -> None:
    ensure(conn)
    conn.execute("UPDATE app_state SET learning_paused = ?, updated_at = ? WHERE id = 1", (int(paused), _now()))


def mark_learning_reset(conn: sqlite3.Connection, at: str | None = None) -> str:
    ensure(conn)
    at = at or _now()
    conn.execute("UPDATE app_state SET learning_reset_at = ?, updated_at = ? WHERE id = 1", (at, _now()))
    return at


def current(db_path: str) -> AppState:
    """Open, read (creating the row if needed) and close."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            return read(conn)
    finally:
        conn.close()
