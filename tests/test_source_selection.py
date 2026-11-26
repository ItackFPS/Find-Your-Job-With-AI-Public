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
    {"name": "ok", "type": "bundesagentur"},
    {"name": "ok2", "type": "bundesagentur"},
    {"name": "old", "type": "apec"},
    {"name": "nocred", "type": "france_travail"},
    {"name": "tripped", "type": "bundesagentur"},
    {"name": "off", "type": "bundesagentur", "active": False},
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
                                       "reason": REMOVED_SOURCE_TYPES["apec"], "reason_key": "removed"}
    assert "reason_key" not in sel.not_run_rows["nocred"]               # free text: shown as recorded
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


def test_fixed_skip_reasons_carry_a_key_for_the_interface():
    # Phase B B11: the English reason reached the French UI; the key lets it be translated.
    from src.collectors import freeze
    from src.pipeline.preflight import _FT_API_RESTRICTED, _INDEED_OFF
    from src.pipeline.source_selection import reason_key

    assert reason_key(_FT_API_RESTRICTED) == "ft_api_restricted"
    assert reason_key(_INDEED_OFF) == "indeed_off"
    assert reason_key(freeze.FROZEN_REASON) == "frozen"
    assert reason_key(REMOVED_SOURCE_TYPES["wttj"]) == "removed"
    assert reason_key("HTTP 403 from the site") is None


def test_france_travail_login_failure_reasons_carry_a_translated_key():
    """#303: each France Travail login failure class has a key, and both locales say it."""
    import json
    from pathlib import Path

    from src.collectors.france_travail import FT_AUTH_FAILURE_CLASSES
    from src.pipeline.preflight import FT_AUTH_SKIP_REASONS
    from src.pipeline.source_selection import reason_key

    assert set(FT_AUTH_SKIP_REASONS) == set(FT_AUTH_FAILURE_CLASSES)
    locales = Path(__file__).resolve().parents[1] / "frontend" / "src" / "locales"
    for failure_class, text in FT_AUTH_SKIP_REASONS.items():
        assert reason_key(text) == failure_class
        for lang in ("en", "fr"):
            strings = json.loads((locales / lang / "translation.json").read_text(encoding="utf-8"))
            assert strings["sourceStatus"]["reason"][failure_class]
