"""One history row per logical scan (issue #86) — HelloWork.

A scan interrupted by a backend/PC restart, a graceful shutdown or a user
pause is the same logical scan when it resumes: its run row goes
running → interrupted/paused → running → … → completed instead of leaving a
chain of 'failed' rows behind. Genuine errors still record 'failed', and a
failed run is never reopened.

A process death is simulated with a BaseException the scanners' ``except
Exception`` cannot catch: the run row and checkpoint stay 'running', exactly
as when the process is killed, and startup recovery then runs on a fresh
storage instance. HelloWorkCollector.collect is mocked — no
network access.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import patch

import pytest

from src.scan_control import (
    STALE_RUN_ERROR,
    recovered_run_status,
    resumable_scan_id,
)
from tests.test_hellowork_scanner import _raw_job


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
