"""Replay a pipeline run on an isolated database copy, collection fed from stored payloads.

What is real: the product's own run path (``PipelineRunner._execute``: staged-source snapshots,
``run_pipeline``, score and rejection persistence, run completion, retention, self-monitoring).
What is replaced: each configured source's collector returns the raw payloads of a corpus instead of
fetching them. While a replay runs, any socket connection to a non-loopback address raises
:class:`NetworkBlocked`, so a replay can never contact a job source, even by mistake.

Limits, stated in every report this harness produces:

* a corpus built from a database holds the offers that database **stored** (the ``jobs`` table, i.e.
  offers accepted at least once); offers that were only ever rejected are not in it, so a replay run
  is not the same input as the live run that produced the database;
* the embedding model is read from the local cache only (the network guard blocks a download).
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.collectors.base import BaseCollector, CollectorResult


class NetworkBlocked(ConnectionError):
    """A replay tried to open a connection to a non-loopback address."""


# -- corpus ------------------------------------------------------------------------------------------

@dataclass
class ReplayCorpus:
    """Raw collector payloads per source name (the dicts ``normalize_job`` receives)."""

    by_source: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_source.values())

    @classmethod
    def from_database(cls, db_path: str | Path, *, statuses: tuple[str, ...] = ("active",)) -> ReplayCorpus:
        """Payloads of the offers stored in ``db_path`` with one of ``statuses``. The file is opened
        read-only and immutable: the source database is never written."""
        uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            marks = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT source, raw_payload FROM jobs WHERE status IN ({marks}) ORDER BY source, id",
                statuses).fetchall()
        finally:
            conn.close()
        corpus = cls()
        for source, payload in rows:
            try:
                raw = json.loads(payload or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and raw:
                corpus.by_source.setdefault(source, []).append(raw)
        return corpus

    @classmethod
    def from_jsonl(cls, path: str | Path, source: str) -> ReplayCorpus:
        """One raw payload per line, all for ``source``."""
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return cls({source: [json.loads(line) for line in lines if line.strip()]})


def _replay_collector_class(corpus: ReplayCorpus) -> type[BaseCollector]:
    class ReplayCollector(BaseCollector):
        """Serves the corpus payloads of the source it is built for; reads nothing else."""

        def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
            super().__init__()
            self._name = str((config or {}).get("name", ""))

        def collect(self) -> CollectorResult:
            jobs = [dict(p) for p in corpus.by_source.get(self._name, [])]
            return CollectorResult(source_name=self._name, jobs=jobs, metadata={"replay": True})

    return ReplayCollector


# -- network guard -----------------------------------------------------------------------------------

def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, bytes):
        host = host.decode()
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@contextmanager
def no_network() -> Iterator[None]:
    """Refuse every socket connection to a non-loopback address for the duration of the block."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise NetworkBlocked(f"replay: connection to {address!r} refused (no job source may be contacted)")
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise NetworkBlocked(f"replay: connection to {address!r} refused (no job source may be contacted)")
        return real_connect_ex(self, address)

    with patch.object(socket.socket, "connect", connect), patch.object(socket.socket, "connect_ex", connect_ex):
        yield


# -- run ---------------------------------------------------------------------------------------------

@dataclass
class ReplayResult:
    run_id: str
    status: str
    error: str | None
    wall_seconds: float
    internal_seconds: float
    stage_times: dict[str, float]
    collected: int
    accepted: int
    by_source: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "status": self.status, "error": self.error,
            "wall_seconds": round(self.wall_seconds, 3), "internal_seconds": round(self.internal_seconds, 3),
            "wall_minus_internal_seconds": round(self.wall_seconds - self.internal_seconds, 3),
            "stage_times": {k: round(v, 3) for k, v in self.stage_times.items()},
            "collected": self.collected, "accepted": self.accepted,
            "by_source": {k: {kk: vv for kk, vv in v.items() if kk in ("collected", "accepted", "status")}
                          for k, v in self.by_source.items()},
        }


def copy_database(source_db: str | Path, work_dir: str | Path) -> Path:
    """Copy ``source_db`` (and its ``-wal`` / ``-shm`` files, if any) into ``work_dir``; return the copy.
    The replay only ever writes to the copy."""
    src, dest_dir = Path(source_db), Path(work_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "jobs.db"
    if dest.exists():
        raise FileExistsError(f"{dest} exists: a replay never overwrites a database")
    for suffix in ("", "-wal", "-shm"):
        part = Path(str(src) + suffix)
        if part.exists():
            shutil.copy2(part, Path(str(dest) + suffix))
    return dest


def replay_run(work_db: str | Path, config_dir: str | Path, corpus: ReplayCorpus) -> ReplayResult:
    """One run of the product's run path on ``work_db`` with collection served from ``corpus``."""
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage

    work_db, config_dir = str(work_db), str(config_dir)
    runner = PipelineRunner(db_path=work_db, config_dir=config_dir)
    rs = RunStorage(db_path=work_db)
    rs.init_db()
    run_id = f"replay-{time.strftime('%Y%m%dT%H%M%S')}-{time.perf_counter_ns() % 10**6:06d}"
    rs.create_run(run_id)
    rs.close()
    replay_class = _replay_collector_class(corpus)
    with no_network(), patch("src.collectors.registry.registry.get_collector_class",
                             lambda _type: replay_class):
        t0 = time.perf_counter()
        runner._execute(run_id)
        wall = time.perf_counter() - t0
    rs = RunStorage(db_path=work_db)
    try:
        row = rs.get_run(run_id) or {}
    finally:
        rs.close()
    stats = row.get("stats") or {}
    return ReplayResult(
        run_id=run_id, status=str(row.get("status")), error=row.get("error"), wall_seconds=wall,
        internal_seconds=float(stats.get("duration_seconds") or 0.0),
        stage_times=dict(stats.get("stage_times") or {}), collected=int(stats.get("collected") or 0),
        accepted=int(stats.get("scored") or 0), by_source=dict(stats.get("by_source") or {}),
    )
