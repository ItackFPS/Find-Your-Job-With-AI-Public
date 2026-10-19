"""The shipped config/schedules.yaml never starts a live scan on its own.

A first start runs with the example profile, so an automatic scan would send
queries the user never chose (and LinkedIn/HelloWork scans are long). The
shipped schedules are therefore manual: the backend starting, or a UTC day
rolling over, dispatches nothing; the dashboard's "Scan now" still works and
a user can opt in with startup_policy: run_once_per_day.

Uses a temporary database and a copy of the real file; no data/ is touched.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import yaml

SHIPPED = Path("config/schedules.yaml")


class _RecordingHandler:
    def __init__(self) -> None:
        self.dispatch_calls: list[str] = []

    def is_running(self) -> bool:
        return False

    def dispatch(self):
        from src.scheduler.service import DispatchResult

        ref = str(uuid.uuid4())
        self.dispatch_calls.append(ref)
        return DispatchResult(run_ref=ref, completed=True)


def _entries() -> list[dict]:
    return (yaml.safe_load(SHIPPED.read_text(encoding="utf-8")) or {}).get("schedules") or []


def test_no_shipped_schedule_runs_automatically() -> None:
    entries = _entries()
    assert {e["id"] for e in entries} >= {"linkedin_daily", "hellowork_daily"}
    for entry in entries:
        if entry.get("enabled", True):
            assert entry.get("startup_policy") == "disabled", entry["id"]


def test_startup_and_rollover_ticks_dispatch_nothing(tmp_path: Path) -> None:
    from src.scheduler.service import SchedulerService

    shutil.copy(SHIPPED, tmp_path / "schedules.yaml")
    handlers = {"linkedin": _RecordingHandler(), "hellowork": _RecordingHandler()}
    svc = SchedulerService(
        db_path=str(tmp_path / "scheduler.db"), config_dir=str(tmp_path), handlers=handlers,
    )
    from src.scheduler.storage import SchedulerStorage

    storage = SchedulerStorage(str(tmp_path / "scheduler.db"))
    storage.init_db()
    storage.seed_from_yaml(tmp_path / "schedules.yaml")
    storage.close()

    svc._tick(is_startup=True)
    svc._tick(is_startup=False)
    svc.join_dispatches(timeout=5)

    assert handlers["linkedin"].dispatch_calls == []
    assert handlers["hellowork"].dispatch_calls == []


def test_manual_trigger_still_dispatches(tmp_path: Path) -> None:
    from src.scheduler.service import SchedulerService
    from src.scheduler.storage import SchedulerStorage

    shutil.copy(SHIPPED, tmp_path / "schedules.yaml")
    handler = _RecordingHandler()
    svc = SchedulerService(
        db_path=str(tmp_path / "scheduler.db"), config_dir=str(tmp_path),
        handlers={"hellowork": handler},
    )
    storage = SchedulerStorage(str(tmp_path / "scheduler.db"))
    storage.init_db()
    storage.seed_from_yaml(tmp_path / "schedules.yaml")
    storage.close()

    assert svc.trigger_manual("hellowork_daily") is not None
    svc.join_dispatches(timeout=5)
    assert len(handler.dispatch_calls) == 1
