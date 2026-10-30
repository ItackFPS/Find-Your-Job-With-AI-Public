"""Issue #82: the API behind Settings › Sources and background.

* ``GET /api/sources/overview``: each source's kind, whether it is collected, the
  user's choice for an unofficial one, its latest result and its breaker; a source
  switched off in config/sources.yaml is not offered.
* ``PUT /api/sources/{name}/choice`` records the choice with its date.
* ``GET /api/scheduler/status`` says, per schedule, whether it runs daily, whether
  today's run is still due, and when the next one is.

Temporary databases, config directories and settings files only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

SOURCES_YAML = """sources:
  - name: france-travail-web
    type: france_travail_web
    active: true
  - name: remoteok
    type: remoteok
    active: true
  - name: bundesagentur
    type: bundesagentur
    active: false
  - name: indeed
    type: indeed
    active: true
  - name: hellowork
    type: hellowork
    active: false
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from src.api.app import app
    from src.pipeline import circuit_breaker
    from src.storage.runs import RunStorage

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    db_path = str(tmp_path / "jobs.db")

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    run_id = str(uuid.uuid4())
    rs.create_run(run_id)
    rs.complete_run(run_id, {"by_source": {
        "france-travail-web": {"collected": 40, "accepted": 12, "errors": 0, "status": "completed",
                               "access_class": "public_page"},
        "remoteok": {"collected": 0, "accepted": 0, "errors": 1, "status": "failed",
                     "reason": "The site did not answer"},
    }})
    rs.close()
    state = circuit_breaker.BreakerState(
        source="remoteok", consecutive_failures=3, trips=1,
        paused_until=datetime.now(UTC) + timedelta(hours=2),
        last_failure_reason="timeout", last_failure_at=datetime.now(UTC),
    )
    circuit_breaker.save_states(db_path, [state])

    for key, value in {
        "db_path": db_path,
        "config_dir": str(config_dir),
        "settings_path": str(tmp_path / "settings.json"),
        "linkedin_config": SimpleNamespace(enabled=True),
        "hellowork_scanner": object(),
        "source_opted_in": lambda source: source == "linkedin",
    }.items():
        monkeypatch.setitem(app.state._state, key, value)
    return TestClient(app)          # no lifespan: only the routes under test


def _rows(client: TestClient) -> dict[str, dict]:
    r = client.get("/api/sources/overview")
    assert r.status_code == 200
    return {row["name"]: row for row in r.json()["sources"]}


def test_overview_lists_each_source_with_its_kind_and_state(client: TestClient) -> None:
    rows = _rows(client)
    assert set(rows) == {"france-travail-web", "remoteok", "indeed", "hellowork", "linkedin"}

    ftw = rows["france-travail-web"]
    assert (ftw["access_class"], ftw["collected"], ftw["choice"]) == ("public_page", True, None)
    assert ftw["last_run"]["accepted"] == 12 and ftw["last_run"]["status"] == "completed"

    remoteok = rows["remoteok"]
    assert remoteok["access_class"] == "official"
    assert remoteok["last_run"]["reason"] == "The site did not answer"
    assert remoteok["breaker"]["paused"] is True

    assert rows["indeed"]["access_class"] == "unofficial"
    assert rows["indeed"]["choice"] == {"enabled": False, "chosen_at": None}
    assert rows["indeed"]["collected"] is False
    # LinkedIn: the choice the scheduler acts on (an observed one counts, plan rule I1).
    assert rows["linkedin"]["choice"]["enabled"] is True
    assert rows["linkedin"]["collected"] is True
    assert rows["hellowork"]["available"] is True


def test_a_choice_is_recorded_with_its_date(client: TestClient, tmp_path: Path) -> None:
    r = client.put("/api/sources/indeed/choice", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    chosen_at = r.json()["chosen_at"]
    assert datetime.fromisoformat(chosen_at).tzinfo is not None

    row = _rows(client)["indeed"]
    assert row["choice"] == {"enabled": True, "chosen_at": chosen_at}
    assert row["collected"] is True
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["unofficial_sources"] == {"indeed": True}

    assert client.put("/api/sources/remoteok/choice", json={"enabled": True}).status_code == 404
    assert client.put("/api/sources/indeed/choice", json={"enabled": "yes"}).status_code == 422


# ---------------------------------------------------------------------------
# Scheduler status: daily, due, next run
# ---------------------------------------------------------------------------

class _Handler:
    def is_running(self) -> bool:
        return False

    def dispatch(self):
        from src.scheduler.service import DispatchResult

        return DispatchResult(run_ref=str(uuid.uuid4()), completed=True)


def test_status_says_when_the_next_run_is(tmp_path: Path) -> None:
    from src.scheduler.service import SchedulerService
    from src.scheduler.storage import SchedulerStorage

    (tmp_path / "schedules.yaml").write_text(
        """schedules:
  - id: scan_daily
    source_id: scan
    label: A scan
    enabled: true
    startup_policy: run_once_per_day
    retry_max: 0
    retry_delay_seconds: 300
  - id: manual_only
    source_id: scan
    label: Manual
    enabled: true
    startup_policy: disabled
    retry_max: 0
    retry_delay_seconds: 300
""",
        encoding="utf-8",
    )
    now = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)                  # 10:00 in Paris
    clock = lambda: now  # noqa: E731
    storage = SchedulerStorage(str(tmp_path / "s.db"), clock=clock)
    storage.init_db()
    storage.seed_from_yaml(tmp_path / "schedules.yaml")
    storage.close()
    settings = {"enabled": True, "daily_time": "08:00"}
    svc = SchedulerService(
        db_path=str(tmp_path / "s.db"), config_dir=str(tmp_path), handlers={"scan": _Handler()},
        clock=clock, background=lambda: settings, tz=ZoneInfo("Europe/Paris"),
    )

    def by_id() -> dict[str, dict]:
        return {s["id"]: s for s in svc.get_status()["schedules"]}

    assert (by_id()["scan_daily"]["daily"], by_id()["scan_daily"]["due"]) == (True, True)
    assert by_id()["manual_only"]["daily"] is False

    svc.trigger_manual("scan_daily")
    svc.join_dispatches(timeout=5)
    row = by_id()["scan_daily"]
    assert row["due"] is False
    assert row["next_run_at"] == "2026-10-31T07:00:00+00:00"       # 08:00 Paris, winter time
    assert svc.get_status()["background"] == settings

    settings["enabled"] = False
    assert by_id()["scan_daily"]["daily"] is False
