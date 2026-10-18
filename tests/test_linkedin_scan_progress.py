"""Work-based LinkedIn scan progress and ETA (issue #86).

The percentage and ETA come from the checkpoint, the config and completed
scan history — not from the current run's started_at — so a restart that
resumes the checkpoint neither drops the progress back to ~1% nor resets the
ETA to a full scan's duration.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from src.linkedin.scan_progress import (
    estimate_progress,
    eta_seconds,
    expected_pages,
    seconds_per_page,
)
from tests.test_scan_run_lifecycle import _li_pages, _li_run, _li_scanner, _li_staging

K = [f"q{i}\x1fFrance" for i in range(4)]


def _q(term: str, pages: int, stopped_by: str = "exhaustion") -> dict[str, Any]:
    return {"search_term": term, "location": "France", "pages_fetched": pages, "stopped_by": stopped_by}


def _run(status: str, queries: list[dict[str, Any]], **stats: Any) -> dict[str, Any]:
    return {
        "status": status,
        "started_at": "2026-09-25T00:00:00+00:00",
        "completed_at": "2026-09-25T01:00:00+00:00",
        "stats_json": json.dumps({"queries": queries, **stats}),
    }


# ---------------------------------------------------------------------------
# Expected pages per query
# ---------------------------------------------------------------------------

class TestExpectedPages:
    def test_most_recent_completed_run_where_the_query_ran_wins(self) -> None:
        runs = [  # newest first
            _run("completed", [_q("q0", 0, "resumed_skip"), _q("q1", 7, "error")]),
            _run("failed", [_q("q0", 3)]),
            _run("completed", [_q("q0", 12), _q("q1", 9, "safety_limit")]),
            _run("completed", [_q("q0", 20)]),
        ]
        assert expected_pages(K[:2], runs, max_pages=50) == {K[0]: 12, K[1]: 9}

    def test_median_of_known_counts_for_a_query_without_history(self) -> None:
        runs = [_run("completed", [_q("q0", 10), _q("q1", 20), _q("q2", 15)])]
        assert expected_pages(K, runs, max_pages=50)[K[3]] == 15

    def test_max_pages_when_there_is_no_history_at_all(self) -> None:
        assert expected_pages(K, [], max_pages=50) == {k: 50 for k in K}

    def test_estimate_is_capped_at_the_configured_page_limit(self) -> None:
        runs = [_run("completed", [_q("q0", 40)])]
        assert expected_pages(K[:1], runs, max_pages=25) == {K[0]: 25}

    def test_malformed_history_is_ignored(self) -> None:
        runs = [{"status": "completed", "stats_json": "not json"}, {"status": "completed"}]
        assert expected_pages(K[:1], runs, max_pages=10) == {K[0]: 10}


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

class TestEstimateProgress:
    EXP = {k: 10 for k in K}

    def test_new_scan_starts_at_zero(self) -> None:
        p = estimate_progress(K, set(), None, 0, self.EXP, {})
        assert (p["percent"], p["pages_done_est"], p["pages_total_est"]) == (0, 0, 40)
        assert p["estimated"] is True

    def test_mid_query_checkpoint_is_page_weighted(self) -> None:
        p = estimate_progress(K, {K[0], K[1]}, K[2], 5, self.EXP, {})
        assert (p["pages_done_est"], p["pages_total_est"], p["percent"]) == (25, 40, 62)
        assert p["current_query_pages_est"] == 10

    def test_six_of_twelve_done_and_page_seven_is_about_half_not_one_percent(self) -> None:
        keys = [f"t{i}\x1f" for i in range(12)]
        p = estimate_progress(keys, set(keys[:6]), keys[6], 6, {k: 10 for k in keys}, {})
        assert p["percent"] == 55  # (60 + 6) / 120

    def test_finer_than_completed_queries_over_total(self) -> None:
        before = estimate_progress(K, {K[0]}, K[1], 1, self.EXP, {})["percent"]
        after = estimate_progress(K, {K[0]}, K[1], 8, self.EXP, {})["percent"]
        assert before < after < 50

    def test_query_that_finished_early_counts_its_actual_pages(self) -> None:
        p = estimate_progress(K, {K[0]}, K[1], 0, self.EXP, {K[0]: 2})
        assert (p["pages_done_est"], p["pages_total_est"]) == (2, 32)

    def test_current_query_past_its_estimate_is_never_complete(self) -> None:
        p = estimate_progress(K[:1], set(), K[0], 25, self.EXP, {})
        assert p["current_query_pages_est"] == 26
        assert p["percent"] == 96
        p = estimate_progress(K[:1], set(), K[0], 10_000, self.EXP, {})
        assert p["percent"] == 99  # never 100 while work remains

    def test_hundred_only_when_every_query_is_complete(self) -> None:
        assert estimate_progress(K, set(K), None, 0, self.EXP, {})["percent"] == 100
        assert estimate_progress(K, set(K[:3]), None, 0, self.EXP, {K[3]: 0})["percent"] < 100

    @pytest.mark.parametrize("expected", [{k: 0 for k in K}, {K[0]: 0, K[1]: 50, K[2]: 1, K[3]: 20}])
    def test_zero_and_mixed_page_counts_stay_in_bounds(self, expected) -> None:
        for completed in (set(), {K[0]}, set(K[:3])):
            for page in (0, 1, 60):
                pct = estimate_progress(K, completed, K[3], page, expected, {})["percent"]
                assert 0 <= pct <= 99

    def test_no_queries(self) -> None:
        assert estimate_progress([], set(), None, 0, {}, {})["percent"] == 0


# ---------------------------------------------------------------------------
# ETA
# ---------------------------------------------------------------------------

class TestSecondsPerPage:
    def test_live_rate_after_three_pages(self) -> None:
        assert seconds_per_page(240.0, 3, {}, []) == (80.0, "live")

    def test_checkpoint_rate_before_the_live_rate_is_ready(self) -> None:
        stats = {"active_seconds": 900.0, "queries": {K[0]: {"pages_fetched": 10}}}
        assert seconds_per_page(30.0, 1, stats, []) == (90.0, "checkpoint")

    def test_history_rate_prefers_active_seconds(self) -> None:
        runs = [_run("completed", [], total_pages_fetched=10, active_seconds=600.0)]
        assert seconds_per_page(0.0, 0, {}, runs) == (60.0, "history")

    def test_legacy_history_uses_wall_clock_only_for_runs_that_never_resumed(self) -> None:
        single = _run("completed", [_q("q0", 10)], total_pages_fetched=10)  # 1 h
        resumed = _run("completed", [_q("q0", 0, "resumed_skip")], total_pages_fetched=4)
        assert seconds_per_page(0.0, 0, {}, [resumed]) == (None, None)
        assert seconds_per_page(0.0, 0, {}, [resumed, single]) == (360.0, "history")

    def test_insufficient_data_gives_no_rate(self) -> None:
        tiny = {"active_seconds": 50.0, "queries": {K[0]: {"pages_fetched": 2}}}
        assert seconds_per_page(20.0, 2, tiny, [_run("failed", [], total_pages_fetched=99)]) == (None, None)


class TestEtaSeconds:
    def test_remaining_pages_times_rate(self) -> None:
        p = estimate_progress(K, {K[0]}, K[1], 4, {k: 10 for k in K}, {})
        assert eta_seconds(p, 80.0) == (40 - 14) * 80

    def test_no_rate_no_eta(self) -> None:
        assert eta_seconds(estimate_progress(K, set(), None, 0, {k: 10 for k in K}, {}), None) is None


# ---------------------------------------------------------------------------
# LinkedInScanner.progress() — through real checkpoints
# ---------------------------------------------------------------------------

def _seed_history(staging, pages: int, active_seconds: float) -> None:
    staging.create_scan_run("history-run")
    staging.complete_scan_run(
        "history-run", 0, 0,
        stats_json=json.dumps({
            "total_pages_fetched": pages,
            "active_seconds": active_seconds,
            "queries": [_q("engineer", pages)],
        }),
    )


class _Clock:
    """Controlled stand-in for the scanner module's ``time`` — mocked pages
    finish instantly (and Windows' monotonic clock ticks every ~15 ms), so
    the tests advance time explicitly, as a real page fetch would."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr("src.linkedin.scanner.time", fake)
    return fake


class TestScannerProgress:
    def test_idle_without_checkpoint_has_no_progress(self, tmp_path: Path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        assert _li_scanner(staging, tmp_path).progress() is None

    def test_new_scan_reports_zero_before_its_first_page(self, tmp_path: Path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        scanner = _li_scanner(staging, tmp_path)
        seen: list[dict[str, Any]] = []
        _li_run(scanner, _li_pages(2, on_page=lambda p: seen.append(scanner.progress()) if p == 0 else None))
        assert seen[0]["percent"] == 0
        assert seen[0]["queries_completed"] == 0

    def test_completed_scan_clears_progress(self, tmp_path: Path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        scanner = _li_scanner(staging, tmp_path)
        scan_id = _li_run(scanner, _li_pages(3))
        assert staging.get_scan_run(scan_id)["status"] == "completed"
        assert scanner.progress() is None  # nothing left: the run itself reads 'completed'

    def test_restart_keeps_the_percentage_and_eta_continues_from_remaining_work(
        self, tmp_path: Path, clock: _Clock
    ) -> None:
        db = str(tmp_path / "t.db")
        staging = _li_staging(db)
        _seed_history(staging, pages=10, active_seconds=1000.0)  # 100 s/page, 10 pages
        event = threading.Event()
        scanner = _li_scanner(staging, tmp_path, shutdown_event=event)
        running: list[dict[str, Any]] = []

        def on_page(page: int) -> None:
            running.append(scanner.progress())
            clock.now += 100  # each page takes 100 s
            if page == 5:
                event.set()

        _li_run(scanner, _li_pages(10, on_page=on_page))
        before = scanner.progress()
        assert before["percent"] == 60  # pages 0-5 done of an estimated 10
        assert before["eta_seconds"] is None  # interrupted: no counting-down ETA
        # While running, the ETA was the remaining pages at the history rate.
        assert (running[0]["eta_seconds"], running[0]["eta_basis"]) == (1000, "history")
        staging.close()

        staging = _li_staging(db)  # process restart
        staging.recover_stale_scans()
        scanner = _li_scanner(staging, tmp_path)
        after_restart = scanner.progress()
        assert after_restart["percent"] == before["percent"]  # not back to ~1%

        resumed: list[dict[str, Any]] = []
        def on_resumed_page(page: int) -> None:
            resumed.append(scanner.progress())
            clock.now += 100

        _li_run(scanner, _li_pages(10, on_page=on_resumed_page))
        first = resumed[0]
        assert first["percent"] == 60
        # The new process has no live rate yet: the checkpoint's own rate
        # (six pages of this logical scan) projects the four pages left —
        # not the full ten-page scan duration.
        assert first["eta_basis"] == "checkpoint"
        assert first["eta_seconds"] == 4 * 100

    def test_live_rate_takes_over_after_three_pages(self, tmp_path: Path, clock: _Clock) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        scanner = _li_scanner(staging, tmp_path)
        seen: list[dict[str, Any]] = []

        def on_page(page: int) -> None:
            seen.append(scanner.progress())
            clock.now += 80

        _li_run(scanner, _li_pages(6, on_page=on_page))
        # No history and fewer than 3 pages: no ETA rather than a guess.
        assert [(s["eta_seconds"], s["eta_basis"]) for s in seen[:3]] == [(None, None)] * 3
        assert seen[3]["eta_basis"] == "live"
        assert seen[3]["eta_seconds"] == (10 - 3) * 80  # no history → 10-page cap

    def test_paused_scan_has_no_eta(self, tmp_path: Path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        _seed_history(staging, pages=10, active_seconds=1000.0)
        scanner = _li_scanner(staging, tmp_path)
        _li_run(scanner, _li_pages(10, on_page=lambda p: scanner.request_pause() if p == 2 else None))
        progress = scanner.progress()
        assert scanner.scan_state() == "paused"
        assert progress["percent"] == 30
        assert (progress["eta_seconds"], progress["eta_basis"]) == (None, None)
