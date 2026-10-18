"""Pause/resume helpers shared by the LinkedIn and HelloWork scanners (issue #81).

Both scanners already stop cooperatively at a checkpointed boundary when the
backend shuts down (a shared ``threading.Event``). A user pause reuses exactly
those boundaries: each scanner owns a private pause event, and the loops check
"shutdown OR pause" instead of shutdown alone. What differs is only how the
stop is recorded — see the scanners' ``_do_scan``.

Checkpoint lifecycle states (``*_scan_checkpoint.state``):
  running      -- a scan is (or was, until the process died) making progress
  paused       -- stopped by the user; survives restarts until resumed
  interrupted  -- stopped by shutdown/crash; resumed automatically
  failed       -- stopped by an error; retried under the normal retry policy
  NULL         -- legacy row written before this column existed (interrupted)

Scan-run history (``*_scan_runs.status``, issue #86): one row per logical
scan — running → interrupted/paused → running → … → completed. A restart,
shutdown or pause never records 'failed'; only a genuine error does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Values exposed by the scanners' scan_state() / the status API.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSING = "pausing"
STATE_PAUSED = "paused"
STATE_INTERRUPTED = "interrupted"

# Scan-run history statuses a later attempt may reopen as the same logical
# scan (issue #86). 'failed' and 'completed' are final.
RESUMABLE_RUN_STATUSES = (STATE_INTERRUPTED, STATE_PAUSED)
RECOVERED_RUN_NOTE = "Interrupted because the process stopped; resumes from its checkpoint."
SHUTDOWN_RUN_NOTE = "Stopped for backend shutdown; resumes from its checkpoint."
CONFIG_CHANGED_RUN_NOTE = (
    "Not resumed: the scanner configuration changed, so a new scan started from the beginning."
)


class StopSignal(threading.Event):
    """An Event-compatible view that reads as set when any of ``events`` is set.

    Lets a scanner hand "shutdown OR pause" to code that only accepts one
    ``threading.Event`` (HelloWorkCollector.collect) without changing it.
    ``observed`` records whether a caller ever saw it set, i.e. whether the
    loop actually stopped because of it rather than finishing first.
    """

    def __init__(self, *events: threading.Event) -> None:
        super().__init__()
        self._events = events
        self.observed = False

    def is_set(self) -> bool:
        if any(e.is_set() for e in self._events):
            self.observed = True
            return True
        return False


def wait_for_stop(stop: StopSignal, seconds: float, slice_seconds: float = 0.25) -> bool:
    """Sleep up to ``seconds``, returning True early once ``stop`` is set.

    Replaces a plain ``time.sleep`` in rate-limit waits so a pause or
    shutdown does not have to wait out a 15-20 s delay.
    """
    if stop.is_set():
        return True
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(slice_seconds, remaining))
        if stop.is_set():
            return True
    return False


def config_fingerprint(payload: dict[str, Any]) -> str:
    """Stable short hash of the scanner settings a checkpoint depends on."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def add_checkpoint_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    """``ALTER TABLE <table> ADD COLUMN <column> TEXT``, tolerating only a
    column that already exists (a re-run, or another process that added it
    first). Any other error — a locked database, for one — propagates, so
    init_db fails and is retried instead of leaving the column missing."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def checkpoint_state(checkpoint: dict[str, Any] | None) -> str | None:
    """Lifecycle state of a checkpoint row (None when there is no row)."""
    if checkpoint is None:
        return None
    return checkpoint.get("state") or STATE_INTERRUPTED


def scan_state(
    is_scanning: bool, pause_requested: bool, checkpoint: dict[str, Any] | None
) -> str:
    """The scanner state exposed to the API/UI."""
    if is_scanning:
        return STATE_PAUSING if pause_requested else STATE_RUNNING
    cp_state = checkpoint_state(checkpoint)
    if cp_state == STATE_PAUSED:
        return STATE_PAUSED
    # 'running' while not scanning here means the process that owned it died
    # and startup recovery has not relabelled it yet — same as interrupted.
    if cp_state in (STATE_INTERRUPTED, STATE_RUNNING):
        return STATE_INTERRUPTED
    return STATE_IDLE


def resumable_scan_id(
    checkpoint: dict[str, Any] | None,
    scan_run: dict[str, Any] | None,
    fingerprint: str,
) -> str | None:
    """scan_id of the logical scan a new attempt should continue, else None.

    A scan interrupted by a restart/shutdown or paused by the user is one
    logical scan: the next attempt reopens its history row instead of
    recording a new one (issue #86). That holds only when the checkpoint is
    resumable ('interrupted'/'paused', or a legacy NULL label), was taken
    under the current config, and its run row is itself 'interrupted' or
    'paused'. A 'failed' checkpoint or run is never reused, so a genuine
    failure stays visible in the history while its progress is still
    resumed by a new run.
    """
    if checkpoint is None or scan_run is None:
        return None
    if checkpoint.get("state") not in (None, STATE_INTERRUPTED, STATE_PAUSED):
        return None
    if checkpoint.get("config_fingerprint") not in (None, fingerprint):
        return None
    if scan_run.get("status") not in RESUMABLE_RUN_STATUSES:
        return None
    return str(checkpoint["scan_id"])


def recovered_run_status(checkpoint: dict[str, Any] | None, scan_id: str) -> str:
    """Status for a scan run left 'running' by a process that died (issue #86).

    The run the checkpoint belongs to is recoverable — it resumes from that
    checkpoint — so it becomes 'interrupted' ('paused' if the user's pause
    was durable but its run-row write was not). Any other stale run has no
    progress to resume and keeps the historical 'failed' label.
    """
    if checkpoint is None or checkpoint.get("scan_id") != scan_id:
        return "failed"
    state = checkpoint.get("state")
    if state == STATE_PAUSED:
        return STATE_PAUSED
    if state in (None, STATE_RUNNING, STATE_INTERRUPTED):
        return STATE_INTERRUPTED
    return "failed"


STALE_RUN_ERROR = "Scan interrupted because the process stopped before completion."


def recover_stale_runs(conn: sqlite3.Connection, runs_table: str, checkpoint_table: str) -> int:
    """Relabel scan runs left 'running' by a dead process; returns how many.

    Runs at startup, before any scan thread exists. The caller commits.
    """
    row = conn.execute(f"SELECT * FROM {checkpoint_table} WHERE id = 1").fetchone()
    checkpoint = dict(row) if row else None
    now = datetime.now(UTC).isoformat()
    stale = [r[0] for r in conn.execute(f"SELECT id FROM {runs_table} WHERE status = 'running'")]
    for scan_id in stale:
        status = recovered_run_status(checkpoint, scan_id)
        note = {STATE_INTERRUPTED: RECOVERED_RUN_NOTE, STATE_PAUSED: None}.get(status, STALE_RUN_ERROR)
        conn.execute(
            f"UPDATE {runs_table} SET status = ?, completed_at = ?, error = ? "
            "WHERE id = ? AND status = 'running'",
            (status, now, note, scan_id),
        )
    return len(stale)


def reopen_run(conn: sqlite3.Connection, runs_table: str, scan_id: str) -> bool:
    """Reopen an interrupted/paused run as 'running' (same row, original
    started_at kept); False when there is no such resumable row."""
    cur = conn.execute(
        f"UPDATE {runs_table} SET status = 'running', completed_at = NULL, error = NULL "
        f"WHERE id = ? AND status IN ({','.join('?' * len(RESUMABLE_RUN_STATUSES))})",
        (scan_id, *RESUMABLE_RUN_STATUSES),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_run(
    conn: sqlite3.Connection,
    runs_table: str,
    scan_id: str,
    status: str,
    note: str | None,
    from_statuses: tuple[str, ...],
) -> None:
    """Set a run's status (and note in ``error``) only from ``from_statuses``,
    so a final label written elsewhere is never overwritten."""
    placeholders = ",".join("?" * len(from_statuses))
    conn.execute(
        f"UPDATE {runs_table} SET status = ?, completed_at = ?, error = ? "
        f"WHERE id = ? AND status IN ({placeholders})",
        (status, datetime.now(UTC).isoformat(), note, scan_id, *from_statuses),
    )
    conn.commit()


def interrupted_checkpoint_id(checkpoint: dict[str, Any] | None) -> str | None:
    """Identity of a checkpoint the scheduler may auto-resume, else None.

    Only a checkpoint explicitly labelled 'interrupted' (a cooperative
    shutdown stop, or a 'running' row relabelled by startup recovery after a
    crash) or a legacy row without a state qualifies. A 'running' row left
    behind by a live process means the scan ended without being able to
    label itself — typically its 'failed' label write failed — so it stays
    under the normal retry policy instead (issue #81 review, F2). The value
    changes whenever the checkpoint is written again, which lets the
    scheduler resume each interruption once (F1).
    """
    if checkpoint is None or checkpoint.get("state") not in (None, STATE_INTERRUPTED):
        return None
    return f"{checkpoint.get('scan_id')}|{checkpoint.get('updated_at')}"


def persist_pause(staging: Any, scan_id: str, source: str) -> bool:
    """Durably record a user pause; True only if the checkpoint says 'paused'.

    The checkpoint label is the durable pause (it is what keeps the source
    paused across restarts), so it is written first. If it cannot be
    written, the pause is NOT reported: the stop is recorded like a failed
    run — checkpoint kept, labelled 'failed' when possible — so the normal
    retry policy applies and nothing claims a pause that a restart would not
    honour (issue #81 review, F3).
    """
    try:
        staging.set_checkpoint_state(scan_id, "paused")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "%s scan %s: pause requested but could not be persisted (%s) — recording "
            "the stop as a failed run; progress stays resumable from the checkpoint.",
            source, scan_id, exc,
        )
        try:
            staging.fail_scan_run(
                scan_id,
                f"Pause requested but could not be persisted ({exc}); "
                "resumable from last checkpoint.",
            )
        except Exception as run_exc:  # noqa: BLE001
            logger.warning("%s: could not record the failed pause for scan %s: %s",
                           source, scan_id, run_exc)
        try:
            staging.set_checkpoint_state(scan_id, "failed", create_if_missing=False)
        except Exception as cp_exc:  # noqa: BLE001
            logger.warning(
                "%s: could not label checkpoint 'failed' for scan %s (%s); it stays "
                "'running' and is not auto-resumed until startup recovery relabels it.",
                source, scan_id, cp_exc,
            )
        return False
    try:
        staging.pause_scan_run(scan_id)
    except Exception as exc:  # noqa: BLE001
        # The pause itself is durable (checkpoint 'paused'); only the history
        # row lags, and startup recovery marks a leftover 'running' row paused.
        logger.warning(
            "%s: scan %s paused, but its scan-run row could not be marked paused: %s",
            source, scan_id, exc,
        )
    return True


def record_shutdown_stop(staging: Any, scan_id: str, source: str) -> None:
    """Record a scan run stopped cooperatively for backend shutdown.

    With a checkpoint to resume from, the run is 'interrupted' — the next
    attempt reopens it as the same logical scan (issue #86). Without one (a
    stop before any progress was saved) nothing is resumable, so it keeps
    the historical 'failed' label. Never raises.
    """
    try:
        if staging.get_scan_checkpoint() is not None:
            staging.interrupt_scan_run(scan_id)
        else:
            staging.fail_scan_run(
                scan_id, "Stopped for graceful backend shutdown before any progress was saved."
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: could not record shutdown stop for scan %s: %s", source, scan_id, exc)


def status_fields(scanner: Any) -> tuple[str, dict[str, Any] | None]:
    """(state, progress) for a scanner's status API response.

    Tolerates scanner doubles without the pause/resume methods by reporting
    running/idle from is_scanning() alone.
    """
    if not hasattr(scanner, "scan_state"):
        return (STATE_RUNNING if scanner.is_scanning() else STATE_IDLE), None
    return scanner.scan_state(), scanner.progress()


def resume_scan(scanner: Any, scheduler: Any, source_id: str) -> tuple[str | None, str | None]:
    """Resume a source from its checkpoint: (scan_id, exec_id).

    Prefers the scheduler, so the resumed scan is recorded as today's run
    (otherwise the next tick would start a second full scan from page 0 once
    it completes); falls back to starting the scanner directly when the
    source has no enabled schedule. Raises RuntimeError when the source is
    already running or the backend is shutting down.
    """
    if scanner.is_scanning():
        raise RuntimeError(f"A {source_id} scan is already in progress")
    if scanner.shutdown_requested():
        raise RuntimeError("The backend is shutting down; resume after it restarts")
    if scheduler is not None:
        try:
            exec_id = scheduler.resume_source(source_id)
        except ValueError:
            exec_id = None  # schedule exists but no handler registered
        if exec_id is not None:
            return None, exec_id
    return scanner.start_scan(), None
