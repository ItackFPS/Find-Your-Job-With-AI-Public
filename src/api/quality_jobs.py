"""Background quality jobs: run the existing self-check or an evaluation run from the UI (plan F9.1).

One job at a time, in a worker thread. The work itself is the existing evaluation CLI
(``python -m src.evaluation self-check`` / ``run``) started as a child process, so production code
still never imports ``src.evaluation`` (ADR 0007, ``tests/test_evaluation_isolation.py``) and a
cancel can stop it at once. The report the CLI writes stays the source of truth (``data/benchmarks``);
this module only reads it back as plain JSON and keeps a short summary made of codes and numbers,
which the UI turns into translated sentences.

Job history lives in one additive ``quality_jobs`` table in ``jobs.db`` (nothing else is touched).
A job left ``running`` by a process that is gone is reported ``failed`` / ``interrupted``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The committed synthetic benchmark the self-check uses by default (shipped by the Windows package too).
BENCHMARK_DATASET = PROJECT_ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"

KINDS = ("self_check", "evaluation")
# Phases a running job goes through, in order: the UI shows them as steps.
PHASES = ("starting", "running", "summarising")
_TIMEOUT_SECONDS = 15 * 60
_FEW_SUITABLE = 30


class QualityJobBusy(RuntimeError):
    """Another quality job is still running."""

    def __init__(self, active_id: str) -> None:
        super().__init__(f"quality job {active_id} is still running")
        self.active_id = active_id


class QualityJobCancelled(Exception):
    pass


class QualityJobError(Exception):
    """A failure the UI can explain: ``code`` is a stable identifier, never free text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class QualityJobStore:
    """The ``quality_jobs`` table. One short-lived connection per call: safe from any thread."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quality_jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT,
                options_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                finished_at TEXT,
                summary_json TEXT,
                error_code TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_jobs_created ON quality_jobs(created_at DESC)")
        return conn

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        conn = self._connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def create(self, job_id: str, kind: str, options: dict[str, Any]) -> None:
        self._write("INSERT INTO quality_jobs (id, kind, status, phase, options_json, created_at) "
                    "VALUES (?, ?, 'running', 'starting', ?, ?)", (job_id, kind, json.dumps(options), _now()))

    def set_phase(self, job_id: str, phase: str) -> None:
        self._write("UPDATE quality_jobs SET phase = ? WHERE id = ? AND status = 'running'", (phase, job_id))

    def finish(self, job_id: str, status: str, *, summary: dict[str, Any] | None = None,
               error_code: str | None = None) -> None:
        self._write("UPDATE quality_jobs SET status = ?, phase = NULL, finished_at = ?, summary_json = ?, "
                    "error_code = ? WHERE id = ?",
                    (status, _now(), json.dumps(summary) if summary is not None else None, error_code, job_id))

    def mark_interrupted(self, keep: str | None) -> int:
        """Jobs still ``running`` that no live worker owns (the process that ran them is gone)."""
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE quality_jobs SET status = 'failed', phase = NULL, finished_at = ?, "
                               "error_code = 'interrupted' WHERE status = 'running' AND id IS NOT ?",
                               (_now(), keep))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def get(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM quality_jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return _row_out(row) if row else None

    def list(self, limit: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM quality_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                (limit,)).fetchall()
        finally:
            conn.close()
        return [_row_out(r) for r in rows]


def _row_out(row: sqlite3.Row) -> dict[str, Any]:
    def load(text: str | None) -> Any:
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None
    return {"id": row["id"], "kind": row["kind"], "status": row["status"], "phase": row["phase"],
            "options": load(row["options_json"]) or {}, "created_at": row["created_at"],
            "finished_at": row["finished_at"], "summary": load(row["summary_json"]), "error_code": row["error_code"]}


# ---------------------------------------------------------------------------
# Job context and manager
# ---------------------------------------------------------------------------

@dataclass
class QualityJobContext:
    """What an executor gets: where things live, the options, and how to report progress or stop."""

    job_id: str
    kind: str
    options: dict[str, Any]
    db_path: Path
    config_dir: Path
    self_check_dir: Path
    runs_dir: Path
    store: QualityJobStore
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _process: subprocess.Popen[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_phase(self, phase: str) -> None:
        self.store.set_phase(self.job_id, phase)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise QualityJobCancelled()

    def kill(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.kill()

    def run_process(self, cmd: list[str], *, timeout: float = _TIMEOUT_SECONDS) -> tuple[int, str, str]:
        """Run ``cmd`` from the project root; a cancel kills it. Returns (exit code, stdout, stderr)."""
        self.check_cancelled()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        with self._lock:
            self._process = subprocess.Popen(
                cmd, cwd=str(PROJECT_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            proc = self._process
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancel_event.is_set():
                        proc.kill()
                        proc.communicate()
                        raise QualityJobCancelled() from None
                    if time.monotonic() > deadline:
                        proc.kill()
                        proc.communicate()
                        raise QualityJobError("timeout") from None
        finally:
            with self._lock:
                self._process = None
        self.check_cancelled()
        return proc.returncode, out, err


Executor = Callable[[QualityJobContext], dict[str, Any]]


class QualityJobManager:
    """Runs at most one quality job at a time, each in its own daemon thread."""

    def __init__(self, executors: dict[str, Executor] | None = None) -> None:
        self.executors: dict[str, Executor] = executors or {"self_check": run_self_check_job,
                                                            "evaluation": run_evaluation_job}
        self._lock = threading.Lock()
        self._active: QualityJobContext | None = None
        self._thread: threading.Thread | None = None

    def active_id(self) -> str | None:
        with self._lock:
            return self._active.job_id if self._active else None

    def recover(self, db_path: str | Path) -> int:
        with self._lock:
            keep = self._active.job_id if self._active else None
            return QualityJobStore(db_path).mark_interrupted(keep)

    def start(self, kind: str, options: dict[str, Any], *, db_path: str | Path, config_dir: str | Path,
              self_check_dir: str | Path, runs_dir: str | Path) -> str:
        if kind not in self.executors:
            raise ValueError(f"unknown quality job kind: {kind}")
        store = QualityJobStore(db_path)
        with self._lock:
            if self._active is not None:
                raise QualityJobBusy(self._active.job_id)
            ctx = QualityJobContext(job_id=uuid.uuid4().hex, kind=kind, options=dict(options),
                                    db_path=Path(db_path), config_dir=Path(config_dir),
                                    self_check_dir=Path(self_check_dir), runs_dir=Path(runs_dir), store=store)
            store.create(ctx.job_id, kind, ctx.options)
            self._active = ctx
            self._thread = threading.Thread(target=self._run, args=(ctx,), name=f"quality-{kind}", daemon=True)
            self._thread.start()
        return ctx.job_id

    def cancel(self, job_id: str) -> bool:
        """Ask the running job to stop. False when ``job_id`` is not the running job."""
        with self._lock:
            ctx = self._active
            if ctx is None or ctx.job_id != job_id:
                return False
            ctx.cancel_event.set()
        ctx.kill()
        return True

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running job (if any) has finished: for tests and shutdown."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, ctx: QualityJobContext) -> None:
        try:
            summary = self.executors[ctx.kind](ctx)
            ctx.check_cancelled()
            ctx.store.finish(ctx.job_id, "completed", summary=summary)
        except QualityJobCancelled:
            ctx.store.finish(ctx.job_id, "cancelled")
        except QualityJobError as exc:
            ctx.store.finish(ctx.job_id, "failed", error_code=exc.code)
        except Exception:  # noqa: BLE001 - a job must never take the worker down; logged for diagnosis
            logger.exception("quality job %s (%s) failed", ctx.job_id, ctx.kind)
            ctx.store.finish(ctx.job_id, "failed", error_code="unexpected")
        finally:
            with self._lock:
                if self._active is ctx:
                    self._active = None


# ---------------------------------------------------------------------------
# Default executors: the existing evaluation CLI in a child process
# ---------------------------------------------------------------------------

def _written_path(stderr: str, inside: Path) -> Path:
    """The report path the CLI printed (``written: <path>``), refused unless it is under ``inside``."""
    for line in reversed(stderr.splitlines()):
        if line.startswith("written: "):
            path = Path(line[len("written: "):].strip()).resolve()
            if path.is_relative_to(inside.resolve()) and path.is_file():
                return path
            break
    raise QualityJobError("no_report")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QualityJobError("unreadable_report") from exc
    if not isinstance(data, dict):
        raise QualityJobError("unreadable_report")
    return data


def run_self_check_job(ctx: QualityJobContext) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "src.evaluation", "self-check", "--out", str(ctx.self_check_dir),
           "--config-dir", str(ctx.config_dir)]
    cmd += ["--db", str(ctx.db_path)] if ctx.options.get("include_feedback", True) else ["--no-feedback"]
    ctx.set_phase("running")
    code, _out, err = ctx.run_process(cmd)
    if code not in (0, 1):            # 0 pass, 1 a finding (still a report), 2 the tool failed
        logger.warning("self-check exited %s: %s", code, err[-2000:])
        raise QualityJobError("tool_failed")
    ctx.set_phase("summarising")
    path = _written_path(err, ctx.self_check_dir)
    return summarise_self_check(_read_json(path), report_file=path.name)


def run_evaluation_job(ctx: QualityJobContext) -> dict[str, Any]:
    out_dir = ctx.runs_dir / BENCHMARK_DATASET.name
    cmd = [sys.executable, "-m", "src.evaluation", "run", str(BENCHMARK_DATASET), "--assess",
           "--out", str(out_dir), "--config-dir", str(ctx.config_dir)]
    ctx.set_phase("running")
    code, _out, err = ctx.run_process(cmd)
    if code != 0:
        logger.warning("evaluation run exited %s: %s", code, err[-2000:])
        raise QualityJobError("tool_failed")
    ctx.set_phase("summarising")
    path = _written_path(err, ctx.runs_dir)
    return summarise_evaluation_run(_read_json(path), report_file=path.relative_to(ctx.runs_dir.resolve()).as_posix())


# ---------------------------------------------------------------------------
# Summaries: codes and numbers only (the UI words them, in the user's language)
# ---------------------------------------------------------------------------

def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summarise_feedback(fb: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(fb, dict):
        return {"status": "not_requested"}
    if fb.get("status") != "measured":
        return {"status": "unavailable"}
    contradictions = fb.get("contradictions") or {}
    return {"status": "measured", "answered": _num(fb.get("jobs_with_explicit_feedback")),
            "decided": _num(fb.get("decided_jobs")), "coverage": _num(fb.get("coverage")),
            "contradictions": sum(int((c or {}).get("count") or 0) for c in contradictions.values())}


def _self_check_dataset(e: dict[str, Any]) -> dict[str, Any]:
    ds, q = e.get("dataset") or {}, e.get("eligibility") or {}
    safety, base = e.get("safety") or {}, e.get("baseline") or {}
    review = (e.get("uncertainty") or {}).get("review_usefulness")
    suitable = _num(ds.get("accept_labels"))
    flags: list[str] = []
    if ds.get("kind") == "synthetic":
        flags.append("synthetic_benchmark")
    if suitable is not None and suitable < _FEW_SUITABLE:
        flags.append("few_suitable_labels")
    if set(ds.get("label_provenance") or {}) - {"human"}:
        flags.append("labels_not_independent")
    if review == "insufficient_evidence":
        flags.append("review_insufficient_evidence")
    if base.get("status") in ("missing", "mismatch"):
        flags.append(f"baseline_{base['status']}")
    flips = base.get("flips") or {}
    return {
        "id": ds.get("id"), "version": ds.get("version"), "kind": ds.get("kind"), "n_jobs": _num(ds.get("n_jobs")),
        "suitable": suitable, "status": e.get("status"),
        "unsafe_rejections": len(safety.get("unsafe_rejections") or []),
        "rejections_without_reason": len(safety.get("rejections_without_reason") or []),
        "false_positives": _num(q.get("fp")), "precision": _num(q.get("precision")), "recall": _num(q.get("recall")),
        "ranking": (e.get("ranking") or {}).get("status") or "not_measured", "review": review,
        "baseline": base.get("status"),
        "flips": {"fixed": len(flips.get("fixed") or []), "broken": len(flips.get("broken") or [])} if flips else None,
        "flags": flags,
    }


def summarise_self_check(report: dict[str, Any], *, report_file: str | None = None) -> dict[str, Any]:
    return {"kind": "self_check", "status": report.get("status"), "created_at": report.get("created_at"),
            "datasets": [_self_check_dataset(e) for e in report.get("datasets") or [] if isinstance(e, dict)],
            "feedback": summarise_feedback(report.get("feedback")), "report_file": report_file}


def summarise_evaluation_run(run: dict[str, Any], *, report_file: str | None = None) -> dict[str, Any]:
    ds, m = run.get("dataset") or {}, run.get("metrics") or {}
    o = m.get("overall") or {}
    tp, fn = _num(o.get("tp")), _num(o.get("fn"))
    suitable = tp + fn if tp is not None and fn is not None else None
    review = (m.get("review") or {}).get("status")
    flags: list[str] = []
    if ds.get("kind") == "synthetic":
        flags.append("synthetic_benchmark")
    if suitable is not None and suitable < _FEW_SUITABLE:
        flags.append("few_suitable_labels")
    if (m.get("label_provenance") or {}).get("basis") not in (None, "independent"):
        flags.append("labels_not_independent")
    if review == "insufficient_evidence":
        flags.append("review_insufficient_evidence")
    unsafe = int(fn or 0)
    return {"kind": "evaluation", "status": "safety_violation" if unsafe else "measured",
            "created_at": run.get("created_at"),
            "datasets": [{"id": ds.get("id"), "version": ds.get("version"), "kind": ds.get("kind"),
                          "n_jobs": _num(m.get("n_jobs")), "suitable": suitable, "status": None,
                          "unsafe_rejections": unsafe, "rejections_without_reason": None,
                          "false_positives": _num(o.get("fp")), "precision": _num(o.get("precision")),
                          "recall": _num(o.get("recall")), "ranking": "not_measured", "review": review,
                          "baseline": None, "flips": None, "flags": flags, "warnings": len(run.get("warnings") or [])}],
            "feedback": {"status": "not_requested"}, "report_file": report_file}


def latest_self_check(self_check_dir: Path) -> dict[str, Any] | None:
    """The newest readable self-check report on disk, summarised (whoever ran it: UI or CLI)."""
    best: tuple[str, str, dict[str, Any]] | None = None
    for path in self_check_dir.glob("*.json") if self_check_dir.is_dir() else []:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            key = (str(d["created_at"]), path.name)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if best is None or key > best[:2]:
            best = (*key, d)
    return summarise_self_check(best[2], report_file=best[1]) if best else None


def latest_evaluation(runs_dir: Path) -> dict[str, Any] | None:
    """The newest readable evaluation run on disk (file names start with their UTC timestamp)."""
    paths = sorted(runs_dir.rglob("*.json"), key=lambda p: p.name, reverse=True) if runs_dir.is_dir() else []
    for path in paths[:20]:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and "metrics" in d:
                return summarise_evaluation_run(d, report_file=path.relative_to(runs_dir).as_posix())
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None
