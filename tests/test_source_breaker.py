"""Per-source circuit breaker, parser canary and yield anomaly in a pipeline run (plan
F6.1, F1.3). Temporary databases, fake collectors, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.collectors.base import BaseCollector, CollectorResult, RecordedFixture
from src.pipeline.circuit_breaker import (
    FAILURE_THRESHOLD,
    MAX_PAUSE,
    BreakerState,
    load_states,
    next_state,
    pause_duration,
    save_states,
)

NOW = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)


# -- pure breaker logic ------------------------------------------------------------------


def test_three_failed_runs_in_a_row_pause_the_source() -> None:
    state = BreakerState("eures")
    for i in range(FAILURE_THRESHOLD - 1):
        state = next_state(state, "failed", "HTTP 503", NOW)
        assert state.consecutive_failures == i + 1 and state.paused_until is None
    state = next_state(state, "failed", "HTTP 503", NOW)
    assert state.trips == 1 and state.paused_until == NOW + pause_duration(1)
    assert state.is_paused(NOW + timedelta(hours=1)) and not state.is_paused(state.paused_until)


def test_backoff_doubles_on_each_trip_up_to_a_ceiling() -> None:
    assert [pause_duration(t) for t in (1, 2, 3)] == [timedelta(hours=6), timedelta(hours=12),
                                                      timedelta(hours=24)]
    assert pause_duration(20) == MAX_PAUSE
    tripped = BreakerState("x", consecutive_failures=3, trips=1, paused_until=NOW)
    again = next_state(tripped, "failed", "still down", NOW)  # the retry after the pause failed
    assert again.trips == 2 and again.paused_until == NOW + timedelta(hours=12)


@pytest.mark.parametrize("status", ["completed", "partial"])
def test_one_success_closes_the_breaker(status: str) -> None:
    tripped = BreakerState("x", consecutive_failures=4, trips=2, paused_until=NOW, last_failure_reason="r")
    assert next_state(tripped, status, None, NOW) == BreakerState("x")


@pytest.mark.parametrize("status", ["skipped", "paused", None])
def test_runs_where_the_source_did_not_run_change_nothing(status: str | None) -> None:
    state = BreakerState("x", consecutive_failures=2)
    assert next_state(state, status, None, NOW) is state


def test_state_round_trips_through_sqlite(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    state = next_state(BreakerState("hellowork", consecutive_failures=2), "failed", "parser_changed", NOW)
    save_states(db, [state])
    assert load_states(db) == {"hellowork": state}


def test_unreadable_store_pauses_nothing(tmp_path: Path) -> None:
    assert load_states(str(tmp_path / "missing-dir" / "jobs.db")) == {}


# -- in a pipeline run -------------------------------------------------------------------

_CALLS: dict[str, int] = {}


class _Down(BaseCollector):
    access_class = "official"
    succeed = False

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        _CALLS["down"] = _CALLS.get("down", 0) + 1
        if _Down.succeed:
            return CollectorResult(source_name="down", jobs=[_job("d1")])
        return CollectorResult(source_name="down", jobs=[], errors=["HTTP 503"])


class _ServedButEmpty(BaseCollector):
    """The v1 HelloWork bug: pages answered 200, the parser found nothing, no error."""

    access_class = "public_page"
    robots_policy = True

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("x.html", "<li>Python Developer</li>", lambda text: [{"title": text}])

    def collect(self) -> CollectorResult:
        self._observe_response(httpx.Response(200, text="<html>" + "x" * 20_000 + "</html>"))
        return CollectorResult(source_name="served", jobs=[])


class _Healthy(BaseCollector):
    access_class = "official"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        return CollectorResult(source_name="healthy", jobs=[_job("h1")])


def _job(job_id: str) -> dict[str, Any]:
    return {"source_job_id": job_id, "url": f"https://example.test/{job_id}", "title": "Python Developer",
            "company": "Acme", "location": "Paris", "description": "Python developer position."}


_CLASSES = {"down": _Down, "served": _ServedButEmpty, "healthy": _Healthy}


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch):  # noqa: ANN201
    from src.collectors.registry import registry
    from src.pipeline.orchestrator import run_pipeline

    monkeypatch.setattr(registry, "get_collector_class", lambda src_type: _CLASSES[src_type])
    monkeypatch.setattr(_Down, "succeed", False)
    _CALLS.clear()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(
        "target_domains: [software_development]\ntarget_skills: [python]\n", encoding="utf-8")
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text(
        "sources:\n"
        "  - {name: down, type: down, active: true}\n"
        "  - {name: served, type: served, active: true}\n"
        "  - {name: healthy, type: healthy, active: true}\n", encoding="utf-8")
    db = str(tmp_path / "jobs.db")

    def run() -> Any:
        result = run_pipeline(db_path=db, config_dir=str(config_dir))
        assert result.status == "completed", result.error
        return result.stats.by_source

    run.db = db  # type: ignore[attr-defined]
    return run


def test_breaker_pauses_skips_and_resumes_a_failing_source(pipeline) -> None:  # noqa: ANN001
    save_states(pipeline.db, [BreakerState("down", consecutive_failures=FAILURE_THRESHOLD - 1)])

    tripped = pipeline()["down"]                     # third failure in a row
    assert tripped["status"] == "failed" and tripped["consecutive_failures"] == FAILURE_THRESHOLD
    assert tripped["paused_until"]

    paused = pipeline()                              # skipped while paused, the rest still runs
    assert _CALLS["down"] == 1
    assert paused["down"]["status"] == "paused" and paused["down"]["collected"] == 0
    assert paused["down"]["paused_until"] == tripped["paused_until"]
    assert "3 failed runs in a row (last: HTTP 503)" in paused["down"]["reason"]
    assert paused["healthy"]["status"] == "completed"

    state = load_states(pipeline.db)["down"]          # the pause is over: tried again, and it works
    save_states(pipeline.db, [BreakerState("down", consecutive_failures=state.consecutive_failures,
                                           trips=state.trips, paused_until=datetime.now(UTC) - timedelta(minutes=1))])
    _Down.succeed = True
    resumed = pipeline()["down"]
    assert _CALLS["down"] == 2 and resumed["status"] == "completed"
    assert "consecutive_failures" not in resumed and "paused_until" not in resumed
    assert load_states(pipeline.db)["down"] == BreakerState("down")


def test_served_pages_with_no_offer_are_reported_as_a_parser_change(pipeline) -> None:  # noqa: ANN001
    by_source = pipeline()
    served = by_source["served"]
    assert served["canary"] == "parser_changed" and served["status"] == "failed"
    assert served["reason"].startswith("parser_changed: the source answered")
    assert served["access_class"] == "public_page"
    assert served["consecutive_failures"] == 1       # and it counts for the breaker
    assert "canary" not in by_source["healthy"]


def test_a_sudden_drop_to_zero_is_flagged_in_the_run(pipeline) -> None:  # noqa: ANN001
    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=pipeline.db)
    rs.init_db()
    for i in range(3):                               # three earlier runs where "served" yielded 40
        rs.create_run(f"past-{i}")
        rs.complete_run(f"past-{i}", {"by_source": {"served": {"collected": 40, "accepted": 0, "errors": 0},
                                                    "down": {"collected": 40, "accepted": 0, "errors": 0}}})
    rs.close()
    save_states(pipeline.db, [BreakerState("down", consecutive_failures=3, trips=1,
                                           paused_until=datetime.now(UTC) + timedelta(hours=6))])

    by_source = pipeline()
    anomaly = by_source["served"]["yield_anomaly"]
    assert anomaly["severity"] == "zero" and anomaly["baseline_yield"] == 40.0 and anomaly["is_new"] is True
    assert "yield_anomaly" not in by_source["down"]  # paused: it did not run, its 0 is not a yield
    assert "yield_anomaly" not in by_source["healthy"]


def test_run_api_returns_the_new_fields() -> None:
    from src.api.routes.pipeline import _build_run_stats

    row = {"collected": 0, "accepted": 0, "errors": 1, "status": "paused", "reason": "r",
           "access_class": "public_page", "canary": "parser_changed", "consecutive_failures": 3,
           "paused_until": "2026-10-24T14:00:00+00:00", "request_cap_reached": True,
           "yield_anomaly": {"severity": "zero", "baseline_yield": 40.0}}
    out = _build_run_stats({"by_source": {"served": row}}).by_source["served"].model_dump()
    assert {k: out[k] for k in row} == row
    old = _build_run_stats({"by_source": {"x": {"collected": 1}}}).by_source["x"].model_dump()
    assert old["access_class"] is None and old["paused_until"] is None   # older runs still load
