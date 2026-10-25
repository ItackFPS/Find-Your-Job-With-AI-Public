"""In-memory background jobs for the AI setup routes (model pull, Ollama install, benchmark).

A job runs in its own daemon thread and reports progress through a mutable ``state`` dict;
``DELETE`` sets its cancel event and the worker stops at its next check. Jobs live **only
in memory**: restarting the backend forgets them (a pull is resumed by starting it again,
since Ollama keeps partial downloads). Finished jobs are kept for inspection, capped at
:data:`MAX_FINISHED` per registry.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["running", "succeeded", "failed", "cancelled"]
MAX_FINISHED = 20
ERR_INTERNAL = "internal_error"


class JobCancelled(Exception):
    """Raised by a worker when it notices its cancel event."""


class JobFailed(Exception):
    """A worker failure with a stable error code for the UI (and optional details)."""

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = "running"
    error: str | None = None
    error_params: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **values: Any) -> None:
        with self.lock:
            self.state.update(values)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"id": self.id, "kind": self.kind, "status": self.status, "error": self.error,
                    "error_params": dict(self.error_params), "started_at": self.started_at,
                    "finished_at": self.finished_at, **self.state}


class JobRegistry:
    """Start, look up and cancel jobs of one kind."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, worker: Callable[[Job], None], *, key: str | None = None,
              **initial_state: Any) -> tuple[Job, bool]:
        """Run ``worker(job)`` in a thread. With ``key``, a running job with the same key is
        returned instead of starting a second one; the bool says whether a job was created."""
        with self._lock:
            if key is not None:
                for job in self._jobs.values():
                    if job.status == "running" and job.state.get("key") == key:
                        return job, False
            self._prune()
            job = Job(id=uuid.uuid4().hex, kind=self.kind,
                      state={**initial_state, **({"key": key} if key is not None else {})})
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job, worker), daemon=True,
                                  name=f"ai-{self.kind}-{job.id[:8]}")
        thread.start()
        return job, True

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is not None:
            job.cancel_event.set()
        return job

    def wait(self, job_id: str, timeout: float) -> Job | None:
        """Test helper: wait until the job leaves ``running`` (or the timeout passes)."""
        deadline = time.monotonic() + timeout
        job = self.get(job_id)
        while job is not None and job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        return job

    def _run(self, job: Job, worker: Callable[[Job], None]) -> None:
        status: JobStatus
        error: str | None = None
        params: dict[str, Any] = {}
        try:
            worker(job)
            status = "cancelled" if job.cancel_event.is_set() else "succeeded"
        except JobCancelled:
            status = "cancelled"
        except JobFailed as exc:
            status, error, params = "failed", exc.code, exc.params
        except Exception:  # noqa: BLE001 - a job must end with a status, never a dead thread
            logger.exception("AI %s job %s failed", self.kind, job.id)
            status, error = "failed", ERR_INTERNAL
        with job.lock:
            job.status, job.error, job.error_params = status, error, params
            job.finished_at = time.time()

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.status != "running"),
                          key=lambda j: j.finished_at or 0.0)
        for job in finished[: max(0, len(finished) - MAX_FINISHED + 1)]:
            del self._jobs[job.id]
