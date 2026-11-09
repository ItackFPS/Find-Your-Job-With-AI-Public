"""Which sources run, which get a "not run" row, and how breakers settle (src/pipeline/source_selection.py).
Temporary databases, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.collectors.registry import REMOVED_SOURCE_TYPES
from src.pipeline.circuit_breaker import (
    FAILURE_THRESHOLD,
    BreakerState,
    load_states,
    save_states,
)
from src.pipeline.source_selection import NETWORK_OUTAGE_ERROR, select_sources, settle_breakers

SOURCES: list[dict[str, Any]] = [
    {"name": "ok", "type": "jobicy"},
    {"name": "ok2", "type": "jobicy"},
    {"name": "old", "type": "apec"},
    {"name": "nocred", "type": "france_travail"},
    {"name": "tripped", "type": "remoteok"},
    {"name": "off", "type": "jobicy", "active": False},
]


def _select(db: str, breakers: list[BreakerState] | None = None):
    if breakers:
        save_states(db, breakers)
    return select_sources(
        SOURCES, skipped_types={"france_travail"},
        skip_reasons={"france_travail": "set FT_CLIENT_ID"}, db_path=db)


def test_each_inactive_removed_skipped_or_paused_source_is_classified(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    paused = BreakerState("tripped", consecutive_failures=3, trips=1, paused_until=now + timedelta(hours=2),
                          last_failure_reason="403")
    sel = _select(str(tmp_path / "j.db"), [paused])

    assert [s["name"] for s in sel.runnable] == ["ok", "ok2"]
    assert list(sel.not_run_rows) == ["old", "nocred", "tripped"]      # removed, preflight, paused; "off" absent
    assert sel.not_run_rows["old"] == {"collected": 0, "accepted": 0, "errors": 0, "status": "skipped",
                                       "reason": REMOVED_SOURCE_TYPES["apec"]}
    assert sel.not_run_rows["nocred"]["reason"] == "set FT_CLIENT_ID"
    row = sel.not_run_rows["tripped"]
    assert row["status"] == "paused" and row["consecutive_failures"] == 3
    assert row["paused_until"] == paused.paused_until.isoformat() and "403" in row["reason"]


def test_an_expired_pause_runs_the_source_again(tmp_path: Path) -> None:
    expired = BreakerState("tripped", consecutive_failures=3, trips=1,
                           paused_until=datetime.now(UTC) - timedelta(minutes=1))
    sel = _select(str(tmp_path / "j.db"), [expired])
    assert [s["name"] for s in sel.runnable] == ["ok", "ok2", "tripped"]
    assert "tripped" not in sel.not_run_rows


def test_failures_count_and_the_threshold_pauses_the_source(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    sel = _select(db, [BreakerState("ok", consecutive_failures=FAILURE_THRESHOLD - 1)])
    by_source = {"ok": {"status": "failed", "reason": "boom"}}

    assert settle_breakers(sel, by_source, db) == []

    assert by_source["ok"]["consecutive_failures"] == FAILURE_THRESHOLD and "paused_until" in by_source["ok"]
    assert load_states(db)["ok"].is_paused(sel.now)


def test_a_success_closes_the_breaker_and_untracked_sources_stay_untracked(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    sel = _select(db, [BreakerState("ok", consecutive_failures=1)])

    settle_breakers(sel, {"ok": {"status": "completed"}, "ok2": {"status": "completed"}}, db)

    assert load_states(db)["ok"].consecutive_failures == 0
    assert set(load_states(db)) == {"ok"}


def test_a_network_outage_moves_no_breaker_and_is_reported(tmp_path: Path) -> None:
    db = str(tmp_path / "j.db")
    sel = _select(db, [BreakerState("ok", consecutive_failures=1)])
    down = {"status": "failed", "reason": "dns", "network_unreachable": True}
    by_source = {"ok": dict(down), "ok2": dict(down)}

    assert settle_breakers(sel, by_source, db) == [NETWORK_OUTAGE_ERROR]

    assert by_source["ok"]["network_outage"] is True
    assert load_states(db) == {"ok": BreakerState("ok", consecutive_failures=1)}
