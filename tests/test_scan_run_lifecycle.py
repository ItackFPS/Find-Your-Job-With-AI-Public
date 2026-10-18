"""One history row per logical scan (issue #86) — LinkedIn and HelloWork.

A scan interrupted by a backend/PC restart, a graceful shutdown or a user
pause is the same logical scan when it resumes: its run row goes
running → interrupted/paused → running → … → completed instead of leaving a
chain of 'failed' rows behind. Genuine errors still record 'failed', and a
failed run is never reopened.

A process death is simulated with a BaseException the scanners' ``except
Exception`` cannot catch: the run row and checkpoint stay 'running', exactly
as when the process is killed, and startup recovery then runs on a fresh
storage instance. JobSpy and HelloWorkCollector.collect are mocked — no
network access.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.scan_control import (
    CONFIG_CHANGED_RUN_NOTE,
    RECOVERED_RUN_NOTE,
    STALE_RUN_ERROR,
    recovered_run_status,
    resumable_scan_id,
)
from tests.test_hellowork_scanner import _raw_job
from tests.test_linkedin_scanner import _make_df_row, _make_mock_df


class _ProcessDied(BaseException):
    """Stands in for the process being killed mid-scan."""


def _runs(staging: Any, table: str) -> list[dict[str, Any]]:
    rows = staging._get_conn().execute(f"SELECT * FROM {table} ORDER BY started_at").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Pure helpers (src/scan_control.py)
# ---------------------------------------------------------------------------

class TestResumableScanId:
    CP = {"scan_id": "s1", "state": "interrupted", "config_fingerprint": "fp"}

    @pytest.mark.parametrize("state", ["interrupted", "paused", None])
    def test_resumable_checkpoint_and_run_reuse_the_scan_id(self, state) -> None:
        cp = {**self.CP, "state": state}
        for status in ("interrupted", "paused"):
            assert resumable_scan_id(cp, {"status": status}, "fp") == "s1"

    @pytest.mark.parametrize("status", ["failed", "completed", "running"])
    def test_final_or_live_run_is_never_reused(self, status) -> None:
        assert resumable_scan_id(self.CP, {"status": status}, "fp") is None

    @pytest.mark.parametrize("state", ["failed", "running"])
    def test_failed_or_live_checkpoint_is_never_reused(self, state) -> None:
        assert resumable_scan_id({**self.CP, "state": state}, {"status": "interrupted"}, "fp") is None

    def test_config_change_prevents_reuse(self) -> None:
        assert resumable_scan_id(self.CP, {"status": "interrupted"}, "other") is None

    def test_legacy_checkpoint_without_fingerprint_is_reused(self) -> None:
        cp = {**self.CP, "config_fingerprint": None}
        assert resumable_scan_id(cp, {"status": "interrupted"}, "fp") == "s1"

    def test_missing_checkpoint_or_run(self) -> None:
        assert resumable_scan_id(None, {"status": "interrupted"}, "fp") is None
        assert resumable_scan_id(self.CP, None, "fp") is None


class TestRecoveredRunStatus:
    @pytest.mark.parametrize("state", ["running", "interrupted", None])
    def test_run_owning_the_checkpoint_is_interrupted(self, state) -> None:
        assert recovered_run_status({"scan_id": "s1", "state": state}, "s1") == "interrupted"

    def test_durable_pause_is_kept_paused(self) -> None:
        assert recovered_run_status({"scan_id": "s1", "state": "paused"}, "s1") == "paused"

    def test_failed_checkpoint_keeps_failed(self) -> None:
        assert recovered_run_status({"scan_id": "s1", "state": "failed"}, "s1") == "failed"

    def test_run_without_its_checkpoint_is_failed(self) -> None:
        assert recovered_run_status(None, "s1") == "failed"
        assert recovered_run_status({"scan_id": "other", "state": "running"}, "s1") == "failed"


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------

def _li_staging(db: str):
    from src.linkedin.staging import LinkedInStagingStorage
    store = LinkedInStagingStorage(db_path=db)
    store.init_db()
    return store


def _li_scanner(staging, tmp_path: Path, shutdown_event=None, **cfg_kwargs):
    from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

    cfg_kwargs.setdefault("max_pages_per_query", 10)
    cfg_kwargs.setdefault("max_results_per_query", 1)
    cfg = LinkedInConfig(
        enabled=True,
        rate_limit_seconds=0.0,
        queries=[LinkedInQueryConfig(search_term="engineer", location="France", results_wanted=1)],
        **cfg_kwargs,
    )
    return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path),
                           shutdown_event=shutdown_event)


def _li_run(scanner, scrape) -> str:
    mock_jobspy = MagicMock()
    mock_jobspy.scrape_jobs = scrape
    with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
        return scanner.run_scan()


def _li_pages(total: int, on_page=None):
    """scrape_jobs fake: one job per page for pages 0..total-1, then empty."""
    def scrape(**kwargs):
        page = kwargs["offset"]
        if on_page is not None:
            on_page(page)
        if page >= total:
            return _make_mock_df([])
        return _make_mock_df([_make_df_row(f"lc{page:05d}")])
    return scrape


def _li_die_at(page_to_die: int, total: int):
    def on_page(page):
        if page == page_to_die:
            raise _ProcessDied
    return _li_pages(total, on_page)


class TestLinkedInRunLifecycle:
    TABLE = "linkedin_scan_runs"

    def test_normal_scan_goes_running_to_completed(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        scan_id = _li_run(_li_scanner(staging, tmp_path), _li_pages(3))
        runs = _runs(staging, self.TABLE)
        assert [(r["id"], r["status"]) for r in runs] == [(scan_id, "completed")]
        assert staging.get_scan_checkpoint() is None

    def test_graceful_shutdown_is_interrupted_and_resume_reuses_the_row(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        first = _li_run(
            _li_scanner(staging, tmp_path, shutdown_event=event),
            _li_pages(5, on_page=lambda p: event.set() if p == 1 else None),
        )
        run = staging.get_scan_run(first)
        assert run["status"] == "interrupted"
        assert "shutdown" in run["error"].lower()
        assert staging.get_scan_checkpoint()["state"] == "interrupted"
        started_at = run["started_at"]

        resumed_pages: list[int] = []
        second = _li_run(_li_scanner(staging, tmp_path), _li_pages(5, on_page=resumed_pages.append))

        assert second == first  # same logical scan
        assert resumed_pages[0] == 2  # resumed from the checkpoint, not page 0
        runs = _runs(staging, self.TABLE)
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["started_at"] == started_at  # original start preserved
        assert runs[0]["error"] is None
        assert runs[0]["completed_at"] is not None

    def test_repeated_process_deaths_leave_one_row_and_no_failed_rows(self, tmp_path) -> None:
        db = str(tmp_path / "t.db")
        ids = []
        for die_at in (1, 3, 5):
            staging = _li_staging(db)
            staging.recover_stale_scans()
            with pytest.raises(_ProcessDied):
                _li_run(_li_scanner(staging, tmp_path), _li_die_at(die_at, total=7))
            ids.append(staging.get_scan_checkpoint()["scan_id"])
            assert _runs(staging, self.TABLE)[-1]["status"] == "running"  # as a killed process leaves it
            staging.close()

        staging = _li_staging(db)
        assert staging.recover_stale_scans() == 1
        run = staging.get_scan_run(ids[0])
        assert run["status"] == "interrupted"
        assert run["error"] == RECOVERED_RUN_NOTE
        final = _li_run(_li_scanner(staging, tmp_path), _li_pages(7))

        assert ids == [final] * 3
        runs = _runs(staging, self.TABLE)
        assert [r["status"] for r in runs] == ["completed"]
        assert not [r for r in runs if r["status"] == "failed"]

    def test_startup_recovery_without_checkpoint_keeps_failed(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        staging.create_scan_run("dead-before-first-page")
        assert staging.recover_stale_scans() == 1
        run = staging.get_scan_run("dead-before-first-page")
        assert run["status"] == "failed"
        assert run["error"] == STALE_RUN_ERROR

    def test_startup_recovery_only_relabels_the_checkpoint_owner(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        staging.create_scan_run("orphan")
        staging.create_scan_run("owner")
        staging.save_scan_checkpoint("owner", [], "engineer\x1fFrance", 2)
        assert staging.recover_stale_scans() == 2
        assert staging.get_scan_run("owner")["status"] == "interrupted"
        assert staging.get_scan_run("orphan")["status"] == "failed"
        assert staging.get_scan_checkpoint()["state"] == "interrupted"

    def test_genuine_failure_stays_failed_and_is_not_reused(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        interrupted = _li_run(
            _li_scanner(staging, tmp_path, shutdown_event=event),
            _li_pages(5, on_page=lambda p: event.set() if p == 0 else None),
        )

        failing = _li_scanner(staging, tmp_path)
        with patch.object(failing, "_load_filters_config", side_effect=RuntimeError("boom")):
            failed = _li_run(failing, _li_pages(5))
        assert failed == interrupted  # the resumed attempt itself failed…
        assert staging.get_scan_run(failed)["status"] == "failed"
        assert staging.get_scan_checkpoint()["state"] == "failed"

        resumed_pages: list[int] = []
        retry = _li_run(_li_scanner(staging, tmp_path), _li_pages(5, on_page=resumed_pages.append))
        assert retry != failed  # …so the retry is a new run, the failure stays visible
        assert resumed_pages[0] == 1  # but its progress is still resumed
        statuses = {r["id"]: r["status"] for r in _runs(staging, self.TABLE)}
        assert statuses == {failed: "failed", retry: "completed"}

    def test_pause_then_resume_is_one_logical_scan(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        scanner = _li_scanner(staging, tmp_path)
        paused = _li_run(scanner, _li_pages(5, on_page=lambda p: scanner.request_pause() if p == 1 else None))
        assert staging.get_scan_run(paused)["status"] == "paused"

        resumed = _li_run(_li_scanner(staging, tmp_path), _li_pages(5))
        assert resumed == paused
        assert [r["status"] for r in _runs(staging, self.TABLE)] == ["completed"]

    def test_config_change_closes_the_old_run_and_starts_a_new_one(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        old = _li_run(
            _li_scanner(staging, tmp_path, shutdown_event=event),
            _li_pages(5, on_page=lambda p: event.set() if p == 1 else None),
        )
        pages: list[int] = []
        new = _li_run(_li_scanner(staging, tmp_path, hours_old=24), _li_pages(5, on_page=pages.append))

        assert new != old
        assert pages[0] == 0  # checkpoint discarded, started over
        old_run = staging.get_scan_run(old)
        assert old_run["status"] == "failed"
        assert old_run["error"] == CONFIG_CHANGED_RUN_NOTE
        assert staging.get_scan_run(new)["status"] == "completed"

    def test_scheduler_dispatch_reports_the_same_run_ref_on_resume(self, tmp_path) -> None:
        from src.scheduler.service import LinkedInHandler

        staging = _li_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        scanner = _li_scanner(staging, tmp_path, shutdown_event=event)
        handler = LinkedInHandler(scanner)
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = _li_pages(4, on_page=lambda p: event.set() if p == 1 else None)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            first = handler.dispatch()
        assert first.interrupted and not first.completed

        event.clear()
        mock_jobspy.scrape_jobs = _li_pages(4)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            second = handler.dispatch()
        assert second.completed
        assert second.run_ref == first.run_ref


# ---------------------------------------------------------------------------
# HelloWork
# ---------------------------------------------------------------------------

def _hw_staging(db: str):
    from src.hellowork.staging import HelloWorkStagingStorage
    store = HelloWorkStagingStorage(db_path=db)
    store.init_db()
    return store


def _hw_scanner(staging, shutdown_event=None, **cfg):
    from src.hellowork.scanner import HelloWorkScanner
    return HelloWorkScanner(config=dict(cfg), staging=staging, shutdown_event=shutdown_event)


def _hw_run(scanner, fake) -> str:
    with patch("src.collectors.hellowork.HelloWorkCollector.collect", side_effect=fake):
        return scanner.run_scan()


def _hw_walker(slugs: list[str], seen: list[str] | None = None, on_each=None):
    """collect() fake walking ``slugs`` like the real collector: one job per
    slug, stop checked before each slug, completed slugs skipped."""
    from src.collectors.base import CollectorResult

    def _fake_collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
        for slug in slugs:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            if skip_slugs and slug in skip_slugs:
                continue
            if on_each is not None:
                on_each(slug)
            if seen is not None:
                seen.append(slug)
            on_slug_done(slug, [_raw_job(source_job_id=f"{slug}-1")])
        return CollectorResult(source_name="hellowork", jobs=[], errors=[], duration_seconds=1.0)
    return _fake_collect


def _hw_die_at(slugs: list[str], slug_to_die: str):
    def on_each(slug):
        if slug == slug_to_die:
            raise _ProcessDied
    return _hw_walker(slugs, on_each=on_each)


class TestHelloWorkRunLifecycle:
    TABLE = "hellowork_scan_runs"
    SLUGS = ["a", "b", "c", "d"]

    def test_normal_scan_goes_running_to_completed(self, tmp_path) -> None:
        staging = _hw_staging(str(tmp_path / "t.db"))
        scan_id = _hw_run(_hw_scanner(staging), _hw_walker(self.SLUGS))
        runs = _runs(staging, self.TABLE)
        assert [(r["id"], r["status"], r["jobs_found"]) for r in runs] == [(scan_id, "completed", 4)]

    def test_graceful_shutdown_is_interrupted_and_resume_reuses_the_row(self, tmp_path) -> None:
        staging = _hw_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        first = _hw_run(
            _hw_scanner(staging, shutdown_event=event),
            _hw_walker(self.SLUGS, on_each=lambda s: event.set() if s == "b" else None),
        )
        run = staging.get_scan_run(first)
        assert run["status"] == "interrupted"
        assert "shutdown" in run["error"].lower()
        assert run["jobs_found"] == 2  # running totals saved with each slug
        started_at = run["started_at"]

        seen: list[str] = []
        second = _hw_run(_hw_scanner(staging), _hw_walker(self.SLUGS, seen))

        assert second == first
        assert seen == ["c", "d"]
        runs = _runs(staging, self.TABLE)
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["started_at"] == started_at
        assert runs[0]["error"] is None
        # Totals cover every attempt, not just the last one.
        assert (runs[0]["jobs_found"], runs[0]["jobs_new"]) == (4, 4)

    def test_repeated_process_deaths_leave_one_row_and_no_failed_rows(self, tmp_path) -> None:
        db = str(tmp_path / "t.db")
        ids = []
        for die_at in ("b", "c", "d"):
            staging = _hw_staging(db)
            staging.recover_stale_scans()
            with pytest.raises(_ProcessDied):
                _hw_run(_hw_scanner(staging), _hw_die_at(self.SLUGS, die_at))
            ids.append(staging.get_scan_checkpoint()["scan_id"])
            staging.close()

        staging = _hw_staging(db)
        assert staging.recover_stale_scans() == 1
        assert staging.get_scan_run(ids[0])["status"] == "interrupted"
        seen: list[str] = []
        final = _hw_run(_hw_scanner(staging), _hw_walker(self.SLUGS, seen))

        assert ids == [final] * 3
        assert seen == ["d"]
        runs = _runs(staging, self.TABLE)
        assert [(r["status"], r["jobs_found"]) for r in runs] == [("completed", 4)]

    def test_startup_recovery_without_checkpoint_keeps_failed(self, tmp_path) -> None:
        staging = _hw_staging(str(tmp_path / "t.db"))
        staging.create_scan_run("dead-before-first-slug")
        assert staging.recover_stale_scans() == 1
        run = staging.get_scan_run("dead-before-first-slug")
        assert (run["status"], run["error"]) == ("failed", STALE_RUN_ERROR)

    def test_genuine_failure_stays_failed_and_is_not_reused(self, tmp_path) -> None:
        staging = _hw_staging(str(tmp_path / "t.db"))

        def fail_after_a(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            on_slug_done("a", [_raw_job(source_job_id="a-1")])
            raise RuntimeError("collector exploded")

        failed = _hw_run(_hw_scanner(staging), fail_after_a)
        assert staging.get_scan_run(failed)["status"] == "failed"
        assert staging.get_scan_checkpoint()["state"] == "failed"

        seen: list[str] = []
        retry = _hw_run(_hw_scanner(staging), _hw_walker(self.SLUGS, seen))
        assert retry != failed
        assert seen == ["b", "c", "d"]  # progress still resumed
        statuses = {r["id"]: r["status"] for r in _runs(staging, self.TABLE)}
        assert statuses == {failed: "failed", retry: "completed"}

    def test_pause_then_resume_is_one_logical_scan(self, tmp_path) -> None:
        staging = _hw_staging(str(tmp_path / "t.db"))
        scanner = _hw_scanner(staging)
        paused = _hw_run(scanner, _hw_walker(self.SLUGS, on_each=lambda s: scanner.request_pause() if s == "b" else None))
        assert staging.get_scan_run(paused)["status"] == "paused"

        resumed = _hw_run(_hw_scanner(staging), _hw_walker(self.SLUGS))
        assert resumed == paused
        runs = _runs(staging, self.TABLE)
        assert [(r["status"], r["jobs_found"]) for r in runs] == [("completed", 4)]

    def test_config_change_closes_the_old_run_and_starts_a_new_one(self, tmp_path) -> None:
        staging = _hw_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        old = _hw_run(
            _hw_scanner(staging, shutdown_event=event),
            _hw_walker(self.SLUGS, on_each=lambda s: event.set() if s == "a" else None),
        )
        seen: list[str] = []
        new = _hw_run(_hw_scanner(staging, max_jobs_per_slug=5), _hw_walker(self.SLUGS, seen))

        assert new != old
        assert seen == self.SLUGS
        assert staging.get_scan_run(old)["status"] == "failed"
        assert staging.get_scan_run(new)["status"] == "completed"


# ---------------------------------------------------------------------------
# LinkedIn checkpoint statistics (stats_json)
# ---------------------------------------------------------------------------

def _checkpoint_stats(staging) -> dict[str, Any]:
    import json
    return json.loads(staging.get_scan_checkpoint()["stats_json"])


def _final_stats(staging, scan_id: str) -> dict[str, Any]:
    import json
    return json.loads(staging.get_scan_run(scan_id)["stats_json"])


class TestLinkedInCheckpointStats:
    KEY = "engineer\x1fFrance"

    def test_fresh_database_has_the_stats_column(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        cols = {r["name"] for r in staging._get_conn().execute("PRAGMA table_info(linkedin_scan_checkpoint)")}
        assert "stats_json" in cols

    def test_existing_database_upgrades_in_place_and_keeps_its_checkpoint(self, tmp_path) -> None:
        import sqlite3

        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE linkedin_scan_checkpoint (
                   id INTEGER PRIMARY KEY CHECK (id = 1), scan_id TEXT NOT NULL,
                   completed_queries_json TEXT NOT NULL DEFAULT '[]', current_query_key TEXT,
                   current_page INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                   state TEXT, config_fingerprint TEXT)"""
        )
        conn.execute(
            "INSERT INTO linkedin_scan_checkpoint VALUES (1, 'legacy', '[]', ?, 2, 'ts', 'interrupted', NULL)",
            (self.KEY,),
        )
        conn.commit()
        conn.close()

        staging = _li_staging(db)
        cp = staging.get_scan_checkpoint()
        assert (cp["scan_id"], cp["current_page"], cp["state"]) == ("legacy", 2, "interrupted")
        assert cp["stats_json"] is None  # untouched existing row, NULL stats

        # A NULL-stats checkpoint is still resumable from its saved page.
        pages: list[int] = []
        scan_id = _li_run(_li_scanner(staging, tmp_path), _li_pages(4, on_page=pages.append))
        assert pages[0] == 2
        assert staging.get_scan_run(scan_id)["status"] == "completed"
        assert staging.get_scan_checkpoint() is None

    def test_stats_are_saved_with_every_page(self, tmp_path) -> None:
        staging = _li_staging(str(tmp_path / "t.db"))
        event = threading.Event()
        _li_run(
            _li_scanner(staging, tmp_path, shutdown_event=event),
            _li_pages(9, on_page=lambda p: event.set() if p == 2 else None),
        )
        cp = staging.get_scan_checkpoint()
        stats = _checkpoint_stats(staging)
        assert cp["current_page"] == 3
        assert stats["queries"][self.KEY]["pages_fetched"] == 3  # matches the saved position
        assert stats["queries"][self.KEY]["jobs_staged"] == 3
        assert stats["active_seconds"] >= 0

    def test_statistics_cover_every_attempt_of_the_logical_scan(self, tmp_path) -> None:
        db = str(tmp_path / "t.db")
        staging = _li_staging(db)
        event = threading.Event()
        scan_id = _li_run(
            _li_scanner(staging, tmp_path, shutdown_event=event),
            _li_pages(6, on_page=lambda p: event.set() if p == 1 else None),
        )
        staging.close()

        staging = _li_staging(db)  # "restart"
        staging.recover_stale_scans()
        assert _li_run(_li_scanner(staging, tmp_path), _li_pages(6)) == scan_id

        stats = _final_stats(staging, scan_id)
        assert stats["total_pages_fetched"] == 6  # pages 0-1 and 2-5, not only the last attempt
        assert stats["total_staged"] == 6
        assert stats["queries"][0]["pages_fetched"] == 6
        assert stats["queries"][0]["stopped_by"] == "exhaustion"
        assert "active_seconds" in stats

    def test_page_refetched_after_a_crash_is_counted_once(self, tmp_path) -> None:
        db = str(tmp_path / "t.db")
        staging = _li_staging(db)
        with pytest.raises(_ProcessDied):
            _li_run(_li_scanner(staging, tmp_path), _li_die_at(3, total=5))
        staging.close()

        staging = _li_staging(db)
        staging.recover_stale_scans()
        pages: list[int] = []
        scan_id = _li_run(_li_scanner(staging, tmp_path), _li_pages(5, on_page=pages.append))
        assert pages[0] == 3  # the interrupted page is fetched again (at-least-once)…
        assert _final_stats(staging, scan_id)["total_pages_fetched"] == 5  # …but counted once

    def test_completed_queries_report_their_saved_stats_on_resume(self, tmp_path) -> None:
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

        staging = _li_staging(str(tmp_path / "t.db"))
        cfg = LinkedInConfig(
            enabled=True, rate_limit_seconds=0.0, max_pages_per_query=10, max_results_per_query=1,
            queries=[LinkedInQueryConfig(search_term=t, location="France", results_wanted=1) for t in ("a", "b")],
        )
        event = threading.Event()

        def scrape(**kwargs):
            if kwargs["search_term"] == "b" and kwargs["offset"] == 0:
                event.set()
            if kwargs["offset"] >= 2:
                return _make_mock_df([])
            return _make_mock_df([_make_df_row(f"{kwargs['search_term']}{kwargs['offset']:04d}")])

        first = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path), shutdown_event=event)
        scan_id = _li_run(first, scrape)
        event.clear()
        assert _li_run(LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path)), scrape) == scan_id

        per_query = {q["search_term"]: q for q in _final_stats(staging, scan_id)["queries"]}
        assert per_query["a"]["stopped_by"] == "exhaustion"  # not "resumed_skip" with zeros
        assert per_query["a"]["pages_fetched"] == 2
        assert per_query["b"]["pages_fetched"] == 2
