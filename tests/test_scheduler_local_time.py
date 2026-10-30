"""Issue #80: daily background runs at a local time, with an on/off switch.

* A schedule's day is the period starting at the latest local ``daily_time``:
  before that time nothing new is due; after days with the app closed, one run.
* Across a DST change the run keeps its local wall-clock time.
* ``enabled: false`` stops every automatic run; a manual run still works.
* ``data/settings.json`` ``background`` is validated and exposed at
  ``/api/settings/background``.

Temporary databases, config directories and settings files only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src.app_settings import background_settings, update_background_settings
from src.scheduler.service import DispatchResult, SchedulerService, period_start
from src.scheduler.storage import SchedulerStorage

PARIS = ZoneInfo("Europe/Paris")


# ---------------------------------------------------------------------------
# period_start
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("now,expected", [
    # Summer time (UTC+2): 08:00 local = 06:00 UTC.
    (datetime(2026, 10, 24, 10, 0, tzinfo=UTC), datetime(2026, 10, 24, 6, 0, tzinfo=UTC)),
    # The clocks went back on 2026-10-25: 08:00 local is now 07:00 UTC.
    (datetime(2026, 10, 25, 10, 0, tzinfo=UTC), datetime(2026, 10, 25, 7, 0, tzinfo=UTC)),
    # 07:30 local on that day: still the previous day's period.
    (datetime(2026, 10, 25, 6, 30, tzinfo=UTC), datetime(2026, 10, 24, 6, 0, tzinfo=UTC)),
    # The clocks went forward on 2026-03-29: 08:00 local = 06:00 UTC again.
    (datetime(2026, 3, 29, 9, 0, tzinfo=UTC), datetime(2026, 3, 29, 6, 0, tzinfo=UTC)),
    (datetime(2026, 3, 28, 9, 0, tzinfo=UTC), datetime(2026, 3, 28, 7, 0, tzinfo=UTC)),
])
def test_period_starts_at_the_local_time_across_dst(now: datetime, expected: datetime) -> None:
    assert period_start(now, time(8, 0), PARIS) == expected


def test_period_start_on_the_system_clock_is_at_the_local_time() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    start = period_start(now, time(8, 30))
    assert start <= now < start + timedelta(hours=25)
    assert start.astimezone().time() == time(8, 30)


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Handler:
    def __init__(self) -> None:
        self.dispatch_calls = 0

    def is_running(self) -> bool:
        return False

    def dispatch(self) -> DispatchResult:
        self.dispatch_calls += 1
        return DispatchResult(run_ref=str(uuid.uuid4()), completed=True)


def _service(tmp_path: Path, clock: _Clock, settings: dict) -> tuple[SchedulerService, _Handler]:
    (tmp_path / "schedules.yaml").write_text(
        """schedules:
  - id: scan_daily
    source_id: scan
    label: A scan
    enabled: true
    startup_policy: run_once_per_day
    retry_max: 0
    retry_delay_seconds: 300
""",
        encoding="utf-8",
    )
    storage = SchedulerStorage(str(tmp_path / "scheduler.db"), clock=clock)
    storage.init_db()
    storage.seed_from_yaml(tmp_path / "schedules.yaml")
    storage.close()
    handler = _Handler()
    svc = SchedulerService(
        db_path=str(tmp_path / "scheduler.db"), config_dir=str(tmp_path),
        handlers={"scan": handler}, clock=clock, background=lambda: settings, tz=PARIS,
    )
    return svc, handler


def _tick(svc: SchedulerService, is_startup: bool = False) -> None:
    svc._tick(is_startup=is_startup)
    svc.join_dispatches(timeout=5)


def test_the_next_run_waits_for_the_local_daily_time(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 29, 8, 0, tzinfo=UTC))      # 09:00 local
    svc, handler = _service(tmp_path, clock, {"enabled": True, "daily_time": "08:00"})
    _tick(svc, is_startup=True)
    assert handler.dispatch_calls == 1

    clock.now = datetime(2026, 10, 30, 6, 30, tzinfo=UTC)         # 07:30 local, next day
    _tick(svc)
    assert handler.dispatch_calls == 1                            # not yet

    clock.now = datetime(2026, 10, 30, 7, 0, tzinfo=UTC)          # 08:00 local
    _tick(svc)
    _tick(svc)
    assert handler.dispatch_calls == 2


def test_days_missed_give_one_run(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    svc, handler = _service(tmp_path, clock, {"enabled": True, "daily_time": "08:00"})
    _tick(svc, is_startup=True)
    clock.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    _tick(svc, is_startup=True)
    _tick(svc)
    assert handler.dispatch_calls == 2


def test_switched_off_nothing_runs_automatically_but_manual_runs_do(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    settings = {"enabled": False, "daily_time": "08:00"}
    svc, handler = _service(tmp_path, clock, settings)
    _tick(svc, is_startup=True)
    _tick(svc)
    assert handler.dispatch_calls == 0

    assert svc.trigger_manual("scan_daily") is not None
    svc.join_dispatches(timeout=5)
    assert handler.dispatch_calls == 1

    settings["enabled"] = True                                    # read on every tick
    clock.now += timedelta(days=1)
    _tick(svc)
    assert handler.dispatch_calls == 2


def test_an_unreadable_settings_reader_keeps_the_defaults(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    svc, handler = _service(tmp_path, clock, {})

    def broken() -> dict:
        raise OSError("settings.json locked")

    svc._background = broken
    _tick(svc, is_startup=True)
    assert handler.dispatch_calls == 1


# ---------------------------------------------------------------------------
# Settings storage and API
# ---------------------------------------------------------------------------

def test_defaults_and_invalid_stored_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert background_settings(path) == {"enabled": True, "daily_time": "08:00"}
    path.write_text(json.dumps({"background": {"enabled": "yes", "daily_time": "25:00"}}), encoding="utf-8")
    assert background_settings(path) == {"enabled": True, "daily_time": "08:00"}


def test_update_validates_and_keeps_other_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"unofficial_sources": {"linkedin": True}}), encoding="utf-8")
    assert update_background_settings(path, {"daily_time": "6:5"}) == {"enabled": True, "daily_time": "06:05"}
    assert update_background_settings(path, {"enabled": False})["enabled"] is False
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["unofficial_sources"] == {"linkedin": True}
    assert stored["background"] == {"enabled": False, "daily_time": "06:05"}
    for bad in ({"daily_time": "8h"}, {"enabled": 1}, {"when": "now"}):
        with pytest.raises(ValueError):
            update_background_settings(path, bad)
    assert json.loads(path.read_text(encoding="utf-8"))["background"]["daily_time"] == "06:05"


def test_background_settings_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.app import app

    monkeypatch.setitem(app.state._state, "settings_path", str(tmp_path / "settings.json"))
    client = TestClient(app)        # no lifespan: only the route under test
    assert client.get("/api/settings/background").json() == {"enabled": True, "daily_time": "08:00"}
    r = client.put("/api/settings/background", json={"daily_time": "07:30"})
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "daily_time": "07:30"}
    assert client.put("/api/settings/background", json={"daily_time": "noon"}).status_code == 422
    assert client.get("/api/settings/background").json()["daily_time"] == "07:30"
