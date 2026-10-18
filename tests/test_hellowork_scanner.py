"""Tests for HelloWorkScanner.

The underlying HelloWorkCollector.collect() is mocked throughout — no real
network access. Mirrors tests/test_linkedin_scanner.py's conventions.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.collectors.base import CollectorResult
from src.hellowork.staging import HelloWorkStagingStorage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def staging(tmp_db: str):
    store = HelloWorkStagingStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


@pytest.fixture
def scanner(staging: HelloWorkStagingStorage):
    from src.hellowork.scanner import HelloWorkScanner
    return HelloWorkScanner(config={"metier_slugs": ["developpeur"]}, staging=staging)


def _raw_job(source_job_id: str = "1", title: str = "Développeur Python") -> dict[str, Any]:
    return {
        "title": title,
        "company": "Acme",
        "description": "Build things",
        "location": "Paris, France",
        "countries": ["FR"],
        "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{source_job_id}.html",
        "source_job_id": source_job_id,
        "published_at": "2026-09-01T00:00:00Z",
        "type": "full-time",
        "remote": False,
        "work_mode": None,
        "skills": [],
    }


def _collector_result(jobs: list[dict[str, Any]], errors: list[str] | None = None) -> CollectorResult:
    return CollectorResult(source_name="hellowork", jobs=jobs, errors=errors or [], duration_seconds=1.0)


def _collect_side_effect(jobs: list[dict[str, Any]], errors: list[str] | None = None):
    """Build a collect() side_effect that reports ``jobs`` as one slug's
    results via on_slug_done, mirroring what the real HelloWorkCollector
    does. The scanner stages jobs entirely through that callback now (so
    interrupted collection loses at most one slug's worth of progress,
    not everything) — a bare ``return_value=CollectorResult(jobs=...)``
    would never invoke it and nothing would get staged.
    """
    def _fake_collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
        if on_slug_done is not None:
            on_slug_done("test-slug", jobs)
        return _collector_result(jobs, errors=errors)
    return _fake_collect


class TestHelloWorkScannerBasic:
    def test_is_scanning_false_initially(self, scanner) -> None:
        assert scanner.is_scanning() is False

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_run_scan_returns_scan_id(self, mock_collect: MagicMock, scanner) -> None:
        mock_collect.return_value = _collector_result([])
        scan_id = scanner.run_scan()
        assert scan_id is not None

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_is_scanning_false_after_run_completes(self, mock_collect: MagicMock, scanner) -> None:
        mock_collect.return_value = _collector_result([])
        scanner.run_scan()
        assert scanner.is_scanning() is False

    def test_prevents_concurrent_scans(self, scanner) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_collect(*_args, **_kwargs):
            started.set()
            release.wait(timeout=5)
            return _collector_result([])

        with patch("src.collectors.hellowork.HelloWorkCollector.collect", side_effect=slow_collect):
            t = threading.Thread(target=scanner.run_scan)
            t.start()
            started.wait(timeout=5)
            assert scanner.is_scanning() is True
            second = scanner.run_scan()
            assert second is None
            release.set()
            t.join(timeout=5)
        assert scanner.is_scanning() is False


class TestHelloWorkScannerCrashSafety:
    """Regression coverage mirroring TestLinkedInScannerCrashSafety.

    A transient "database is locked" on create_scan_run() (the first DB
    write _do_scan() makes) used to propagate straight out of the background
    thread, uncaught — silently releasing the scan lock without ever
    recording the scan run. The next trigger then started a brand new
    scan_id from scratch, indistinguishable from the scan having reset to
    the beginning. See src/hellowork/scanner.py::_do_scan.
    """

    def test_run_scan_survives_lock_error_on_create_scan_run(self, scanner, staging) -> None:
        import sqlite3

        def raise_locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(staging, "create_scan_run", side_effect=raise_locked):
            scan_id = scanner.run_scan()  # must not raise

        assert scan_id is not None
        assert scanner.is_scanning() is False

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_scan_can_retry_cleanly_after_a_create_scan_run_failure(
        self, mock_collect: MagicMock, scanner, staging
    ) -> None:
        import sqlite3

        def raise_locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(staging, "create_scan_run", side_effect=raise_locked):
            scanner.run_scan()
        assert scanner.is_scanning() is False

        mock_collect.return_value = _collector_result([])
        scan_id = scanner.run_scan()

        assert scan_id is not None
        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_one_bad_row_does_not_abort_the_whole_scan(
        self, mock_collect: MagicMock, scanner, staging
    ) -> None:
        """A transient per-row staging failure (e.g. a lock timeout while the
        pipeline holds the writer) must be skipped, not let it kill the rest
        of the scan — mirrors LinkedIn's existing per-row error handling in
        src/linkedin/scanner.py::_run_query.
        """
        import sqlite3

        mock_collect.side_effect = _collect_side_effect([
            _raw_job("1", "Développeur Python"),
            _raw_job("2", "Développeur Java"),
            _raw_job("3", "Développeur Go"),
        ])

        real_insert_job = staging.insert_job
        call_count = {"n": 0}

        def flaky_insert_job(record):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise sqlite3.OperationalError("database is locked")
            return real_insert_job(record)

        with patch.object(staging, "insert_job", side_effect=flaky_insert_job):
            scan_id = scanner.run_scan()

        assert scan_id is not None
        row = staging._get_conn().execute(
            "SELECT status, jobs_found FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        # The scan as a whole must still complete, having staged the two
        # good rows around the one transient failure.
        assert row["status"] == "completed"
        assert row["jobs_found"] == 2


class TestHelloWorkScannerPersistence:
    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_scan_inserts_jobs_to_staging(self, mock_collect: MagicMock, scanner, staging) -> None:
        mock_collect.side_effect = _collect_side_effect([_raw_job("1"), _raw_job("2")])
        scanner.run_scan()
        counts = staging.count_by_status()
        assert counts.get("waiting") == 2

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_rescan_does_not_duplicate(self, mock_collect: MagicMock, scanner, staging) -> None:
        mock_collect.side_effect = _collect_side_effect([_raw_job("1")])
        scanner.run_scan()
        scanner.run_scan()
        count = staging._get_conn().execute("SELECT COUNT(*) FROM hellowork_staging").fetchone()[0]
        assert count == 1

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_scan_marks_completed_in_db(self, mock_collect: MagicMock, scanner, staging) -> None:
        mock_collect.side_effect = _collect_side_effect([_raw_job("1")])
        scan_id = scanner.run_scan()
        row = staging.list_scan_runs()[0]
        assert row["id"] == scan_id
        assert row["status"] == "completed"
        assert row["jobs_found"] == 1
        assert row["jobs_new"] == 1

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_scan_failure_recorded_and_not_raised(self, mock_collect: MagicMock, scanner, staging) -> None:
        mock_collect.side_effect = RuntimeError("boom")
        scan_id = scanner.run_scan()
        assert scan_id is not None
        row = staging.list_scan_runs()[0]
        assert row["status"] == "failed"
        assert "boom" in row["error"]

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_empty_results_are_handled_gracefully(self, mock_collect: MagicMock, scanner, staging) -> None:
        mock_collect.return_value = _collector_result([])
        scanner.run_scan()
        row = staging.list_scan_runs()[0]
        assert row["status"] == "completed"
        assert row["jobs_found"] == 0

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_job_missing_title_or_url_is_skipped(self, mock_collect: MagicMock, scanner, staging) -> None:
        bad_job = _raw_job("1")
        bad_job["title"] = ""
        mock_collect.side_effect = _collect_side_effect([bad_job, _raw_job("2")])
        scanner.run_scan()
        counts = staging.count_by_status()
        assert counts.get("waiting") == 1

    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_collector_errors_do_not_fail_the_scan(self, mock_collect: MagicMock, scanner, staging) -> None:
        mock_collect.side_effect = _collect_side_effect([_raw_job("1")], errors=["metier X failed"])
        scan_id = scanner.run_scan()
        row = staging.list_scan_runs()[0]
        assert row["status"] == "completed"
        assert scan_id is not None


class TestSnapshotIntegration:
    @patch("src.collectors.hellowork.HelloWorkCollector.collect")
    def test_full_cycle_insert_snapshot_convert(self, mock_collect: MagicMock, scanner, staging) -> None:
        from src.hellowork.staging import staging_row_to_job

        mock_collect.side_effect = _collect_side_effect([_raw_job("1", title="Développeur Python")])
        scanner.run_scan()

        snapshot = staging.snapshot_waiting("run-1")
        assert len(snapshot) == 1
        job = staging_row_to_job(snapshot[0])
        assert job.title == "Développeur Python"
        assert job.source == "hellowork"
        assert job.source_job_id == "1"

        transferred = staging.mark_transferred_by_stable_ids("run-1", {job.id})
        assert transferred == 1
        counts = staging.count_by_status()
        assert counts.get("transferred") == 1


# ---------------------------------------------------------------------------
# Resumable slug checkpoint
#
# HelloWorkCollector.collect() is mocked here with a fake that mimics its
# real per-slug loop (respecting skip_slugs, invoking on_slug_done) so these
# tests exercise the scanner's checkpoint orchestration without needing live
# HTTP fixtures — the collector's own skip_slugs/on_slug_done contract is
# covered directly in tests/test_hellowork_collector.py.
# ---------------------------------------------------------------------------

class TestHelloWorkScanCheckpointResume:
    def test_checkpoint_persists_slug_position_and_resume_skips_done_slugs(
        self, scanner, staging
    ) -> None:
        all_slugs_jobs = {
            "slug-a": [_raw_job("1", "Job A")],
            "slug-b": [_raw_job("2", "Job B")],
            "slug-c": [_raw_job("3", "Job C")],
        }
        processed_order: list[str] = []

        def fake_collect_crash(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            skip_slugs = skip_slugs or set()
            for slug, jobs in all_slugs_jobs.items():
                if slug in skip_slugs:
                    continue
                processed_order.append(slug)
                if on_slug_done is not None:
                    on_slug_done(slug, jobs)
                if slug == "slug-b":
                    raise RuntimeError("simulated crash mid-collection")
            return _collector_result([])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=fake_collect_crash
        ):
            scan_id_1 = scanner.run_scan()

        assert processed_order == ["slug-a", "slug-b"]

        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id_1,)
        ).fetchone()
        assert row["status"] == "failed"

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert set(json.loads(cp["completed_slugs_json"])) == {"slug-a", "slug-b"}

        count_before = staging._get_conn().execute(
            "SELECT COUNT(*) FROM hellowork_staging"
        ).fetchone()[0]
        assert count_before == 2  # slug-a and slug-b's jobs were staged before the crash

        # --- Resume: a brand new scan (the app restarted / retried) ---
        processed_order.clear()

        def fake_collect_resume(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            skip_slugs = skip_slugs or set()
            for slug, jobs in all_slugs_jobs.items():
                if slug in skip_slugs:
                    continue
                processed_order.append(slug)
                if on_slug_done is not None:
                    on_slug_done(slug, jobs)
            return _collector_result([])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=fake_collect_resume
        ):
            scan_id_2 = scanner.run_scan()

        # Only slug-c was (re)processed — slug-a/slug-b were skipped.
        assert processed_order == ["slug-c"]

        row2 = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id_2,)
        ).fetchone()
        assert row2["status"] == "completed"

        # Full clean completion clears the checkpoint for the next scheduled scan.
        assert staging.get_scan_checkpoint() is None

        # All three jobs present exactly once — no duplicates from resuming,
        # nothing lost from the crash.
        rows = staging._get_conn().execute(
            "SELECT source_job_id, COUNT(*) as n FROM hellowork_staging GROUP BY source_job_id"
        ).fetchall()
        counts = {r["source_job_id"]: r["n"] for r in rows}
        assert counts == {"1": 1, "2": 1, "3": 1}

    def test_checkpoint_not_reset_by_concurrent_api_style_reads(self, scanner, staging) -> None:
        """Regression: reading scan status (what the dashboard polls) while
        a checkpoint exists must never reset it."""
        staging.save_scan_checkpoint("scan-x", ["slug-a", "slug-b"])

        for _ in range(20):
            staging.get_status()
            staging.list_scan_runs()
            staging.count_by_status()

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert set(json.loads(cp["completed_slugs_json"])) == {"slug-a", "slug-b"}


# ---------------------------------------------------------------------------
# Confirming HelloWork's slug-level checkpoint is already safe
#
# Audit (2026-09-11): checked whether HelloWork has the same "checkpoint
# marks failed work as complete" gap found and fixed in LinkedIn's
# per-query handling. It does not — a slug is only ever checked off
# (on_slug_done called) after it fully succeeds; a slug whose listing
# fetch fails is never reported to the scanner as done at all (verified
# at the collector level in
# tests/test_hellowork_collector.py::TestResumableCheckpointHooks::
# test_listing_failure_does_not_trigger_on_slug_done). These tests confirm
# that behavior holds through the *scanner's* checkpoint end-to-end, and
# that an unexpected exception while staging a slug's jobs also does not
# mark that slug complete. No production code change was needed here.
# ---------------------------------------------------------------------------

class TestHelloWorkSlugLevelCheckpointIsSafe:
    def test_a_slug_whose_listing_fails_is_not_checked_off(self, scanner, staging) -> None:
        """The collector's own on_slug_done contract already skips a slug
        whose listing fetch failed; confirm this holds through the real
        (unmocked) HelloWorkCollector, driven by the scanner end-to-end."""
        import httpx

        scanner.config = {"metier_slugs": ["broken-slug"]}
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=httpx.HTTPError("HTTP 500"),
        ), patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit"):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        # The scan itself completes (a per-slug listing failure is recorded
        # as a collector error, not an aborted scan) — but the checkpoint
        # must not claim the failed slug was processed.
        assert row["status"] == "completed"
        assert staging.get_scan_checkpoint() is None  # cleared on clean finish

    def test_an_unexpected_staging_exception_does_not_check_off_the_slug(
        self, scanner, staging
    ) -> None:
        """If something outside the already-isolated per-job try/except
        raises while staging a slug's jobs (e.g. a bug, or a DB failure not
        already caught), that slug must not be recorded as completed — it
        should remain eligible for a retry, not silently skipped forever.
        slug-good is processed first so there is a checkpoint row to
        inspect; slug-bad's failure aborts the rest of that collection
        attempt (a real, accepted side effect — see the class docstring),
        so this deliberately uses two slugs to observe both outcomes.
        """

        def fake_collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            skip_slugs = skip_slugs or set()
            if "slug-good" not in skip_slugs:
                on_slug_done("slug-good", [_raw_job("1", "Good Job")])
            if "slug-bad" not in skip_slugs:
                # A malformed job record (not a dict) — realistic stand-in
                # for an unexpected data/transform error while staging.
                on_slug_done("slug-bad", [None])  # type: ignore[list-item]
            return _collector_result([])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=fake_collect
        ):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "failed"

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        completed = json.loads(cp["completed_slugs_json"])
        assert "slug-good" in completed  # genuinely finished — checked off
        assert "slug-bad" not in completed  # only failed — must stay resumable

        # Resume: slug-good must not be re-processed; slug-bad must be retried.
        processed: list[str] = []

        def fake_collect_resume(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            skip_slugs = skip_slugs or set()
            if "slug-good" not in skip_slugs:
                processed.append("slug-good")
                on_slug_done("slug-good", [_raw_job("1", "Good Job")])
            if "slug-bad" not in skip_slugs:
                processed.append("slug-bad")
                on_slug_done("slug-bad", [_raw_job("2", "Recovered Job")])
            return _collector_result([])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=fake_collect_resume
        ):
            scan_id_2 = scanner.run_scan()

        assert processed == ["slug-bad"]  # slug-good was skipped via checkpoint
        row2 = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id_2,)
        ).fetchone()
        assert row2["status"] == "completed"
        assert staging.get_scan_checkpoint() is None
        rows = staging._get_conn().execute(
            "SELECT source_job_id FROM hellowork_staging"
        ).fetchall()
        assert {r["source_job_id"] for r in rows} == {"1", "2"}  # both recovered, nothing lost


class TestGracefulShutdown:
    """Regression coverage for the backend-shutdown cooperation added after
    the incident where a `uvicorn --reload` restart abruptly killed the
    in-flight LinkedIn scan thread (see tests/test_linkedin_scanner.py's
    TestGracefulShutdown for the original). HelloWorkScanner gets the exact
    same lifecycle semantics via the same shutdown_event mechanism: checked
    between slugs (and, inside HelloWorkCollector, between job-detail
    requests within a slug — see tests/test_hellowork_collector.py's
    TestGracefulShutdown for that level), never marks a scan "completed"
    when it was actually interrupted, never clears/corrupts the checkpoint.
    """

    def _make_scanner(self, staging, shutdown_event=None, **cfg_kwargs):
        from src.hellowork.scanner import HelloWorkScanner
        config = {"metier_slugs": ["slug-a", "slug-b"], **cfg_kwargs}
        return HelloWorkScanner(config=config, staging=staging, shutdown_event=shutdown_event)

    def test_shutdown_mid_scan_does_not_mark_scan_completed_or_clear_checkpoint(self, staging) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, shutdown_event=event)

        def collect_then_shutdown(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            # Mirrors what the real HelloWorkCollector does: slug-a fully
            # completes (staged + checked off) before the shutdown request
            # is noticed; slug-b is never started.
            if on_slug_done is not None:
                on_slug_done("slug-a", [_raw_job("1", "Job A")])
            event.set()
            return _collector_result([_raw_job("1", "Job A")])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=collect_then_shutdown
        ):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status, error FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "interrupted"  # resumable, not "completed" and not "failed" (#86)
        assert "shutdown" in row["error"].lower()

        # Checkpoint must survive with slug-a checked off, so the next scan
        # resumes at slug-b instead of losing progress or starting over.
        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert json.loads(cp["completed_slugs_json"]) == ["slug-a"]

        count = staging._get_conn().execute("SELECT COUNT(*) FROM hellowork_staging").fetchone()[0]
        assert count == 1  # slug-a's job was staged before the shutdown stop, nothing lost

    def test_shutdown_before_any_slug_leaves_checkpoint_untouched(self, staging) -> None:
        event = threading.Event()
        event.set()
        scanner = self._make_scanner(staging, shutdown_event=event)

        mock_collect = MagicMock(side_effect=lambda on_slug_done=None, skip_slugs=None, shutdown_event=None: _collector_result([]))
        with patch("src.collectors.hellowork.HelloWorkCollector.collect", mock_collect):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status, error FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "shutdown" in row["error"].lower()
        assert staging.get_scan_checkpoint() is None

    def test_scan_resumes_correctly_after_a_shutdown_stop(self, staging) -> None:
        """End-to-end: a scan stopped for shutdown after slug-a, then a
        fresh scan (new process/instance, no shutdown_event set) must
        resume at slug-b -- the same resumability already proven for crashes
        in TestHelloWorkScanCheckpointResume, now also proven for a
        cooperative shutdown stop."""
        event = threading.Event()
        scanner = self._make_scanner(staging, shutdown_event=event)

        def collect_then_shutdown(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            if on_slug_done is not None:
                on_slug_done("slug-a", [_raw_job("1", "Job A")])
            event.set()
            return _collector_result([_raw_job("1", "Job A")])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=collect_then_shutdown
        ):
            scanner.run_scan()

        # --- Fresh scanner instance, no shutdown requested (the "next process") ---
        fresh_scanner = self._make_scanner(staging)
        processed: list[str] = []

        def resumed_collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
            skip_slugs = skip_slugs or set()
            assert "slug-a" in skip_slugs  # already completed, must not be re-processed
            processed.append("slug-b")
            if on_slug_done is not None:
                on_slug_done("slug-b", [_raw_job("2", "Job B")])
            return _collector_result([_raw_job("2", "Job B")])

        with patch(
            "src.collectors.hellowork.HelloWorkCollector.collect", side_effect=resumed_collect
        ):
            scan_id = fresh_scanner.run_scan()

        assert processed == ["slug-b"]

        rows = staging._get_conn().execute(
            "SELECT source_job_id FROM hellowork_staging"
        ).fetchall()
        assert {r["source_job_id"] for r in rows} == {"1", "2"}  # both present, nothing lost/duplicated

        row = staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert staging.get_scan_checkpoint() is None


# ---------------------------------------------------------------------------
# Manual pause / resume (issue #81)
# ---------------------------------------------------------------------------

def _slug_walker(slugs, seen: list[str], on_each=None):
    """A collect() fake that walks ``slugs`` exactly like the real collector:
    the stop signal is checked before each slug, completed slugs are skipped,
    and each finished slug is reported through on_slug_done."""
    def _fake_collect(on_slug_done=None, skip_slugs=None, shutdown_event=None):
        for slug in slugs:
            if shutdown_event is not None and shutdown_event.is_set():
                break
            if skip_slugs and slug in skip_slugs:
                continue
            seen.append(slug)
            if on_each is not None:
                on_each(slug)
            on_slug_done(slug, [_raw_job(source_job_id=f"{slug}-1")])
        return _collector_result([])
    return _fake_collect


class TestManualPause:
    def _make_scanner(self, staging, shutdown_event=None, **cfg):
        from src.hellowork.scanner import HelloWorkScanner

        return HelloWorkScanner(config=dict(cfg), staging=staging, shutdown_event=shutdown_event)

    @staticmethod
    def _run(scanner, fake) -> str:
        with patch("src.collectors.hellowork.HelloWorkCollector.collect", side_effect=fake):
            return scanner.run_scan()

    @staticmethod
    def _run_status(staging, scan_id: str) -> str:
        return staging._get_conn().execute(
            "SELECT status FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()["status"]

    def test_pause_stops_at_slug_boundary_and_persists_paused(self, staging) -> None:
        scanner = self._make_scanner(staging)
        seen: list[str] = []

        def on_each(slug):
            if slug == "b":
                assert scanner.request_pause() is True

        scan_id = self._run(scanner, _slug_walker(["a", "b", "c", "d"], seen, on_each))
        assert seen == ["a", "b"]  # slug b finished, c never started
        assert self._run_status(staging, scan_id) == "paused"
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert json.loads(cp["completed_slugs_json"]) == ["a", "b"]
        assert scanner.last_scan_paused() is True
        assert scanner.last_scan_completed() is False
        assert scanner.scan_state() == "paused"
        assert scanner.progress() == {"slugs_completed": 2, "updated_at": cp["updated_at"]}

    def test_resume_skips_completed_slugs_and_completes(self, staging) -> None:
        scanner = self._make_scanner(staging)
        self._run(scanner, _slug_walker(["a", "b", "c"], [], lambda s: s == "a" and scanner.request_pause()))

        seen: list[str] = []
        scan_id = self._run(scanner, _slug_walker(["a", "b", "c"], seen))
        assert seen == ["b", "c"]
        assert self._run_status(staging, scan_id) == "completed"
        assert staging.get_scan_checkpoint() is None
        count = staging._get_conn().execute("SELECT COUNT(*) FROM hellowork_staging").fetchone()[0]
        assert count == 3

    def test_pause_survives_restart(self, staging) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage

        scanner = self._make_scanner(staging)
        self._run(scanner, _slug_walker(["a", "b"], [], lambda s: scanner.request_pause()))

        staging2 = HelloWorkStagingStorage(db_path=str(staging._db_path))
        staging2.recover_stale_scans()
        scanner2 = self._make_scanner(staging2)
        assert scanner2.scan_state() == "paused"
        seen: list[str] = []
        self._run(scanner2, _slug_walker(["a", "b"], seen))
        assert seen == ["b"]
        staging2.close()

    def test_pause_when_idle_and_after_completion_is_a_noop(self, staging) -> None:
        scanner = self._make_scanner(staging)
        assert scanner.request_pause() is False
        scan_id = self._run(scanner, _slug_walker(["a"], []))
        assert scanner.request_pause() is False
        assert self._run_status(staging, scan_id) == "completed"
        assert scanner.scan_state() == "idle"

    def test_pause_after_the_last_slug_completes_normally(self, staging) -> None:
        """Completion immediately before the pause: the collector never saw
        the stop, so the scan completes instead of pausing."""
        scanner = self._make_scanner(staging)
        scan_id = self._run(scanner, _slug_walker(["a"], [], lambda s: scanner.request_pause()))
        assert self._run_status(staging, scan_id) == "completed"
        assert staging.get_scan_checkpoint() is None
        assert scanner.last_scan_paused() is False

    def test_pause_twice_is_idempotent(self, staging) -> None:
        scanner = self._make_scanner(staging)

        def on_each(slug):
            assert scanner.request_pause() is True
            assert scanner.request_pause() is True
            assert scanner.scan_state() == "pausing"

        scan_id = self._run(scanner, _slug_walker(["a", "b"], [], on_each))
        assert self._run_status(staging, scan_id) == "paused"

    def test_pause_and_shutdown_together_records_paused(self, staging) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, shutdown_event=event)

        def on_each(slug):
            scanner.request_pause()
            event.set()

        scan_id = self._run(scanner, _slug_walker(["a", "b"], [], on_each))
        assert self._run_status(staging, scan_id) == "paused"
        assert staging.get_scan_checkpoint()["state"] == "paused"

    def test_shutdown_alone_marks_checkpoint_interrupted(self, staging) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, shutdown_event=event)
        scan_id = self._run(scanner, _slug_walker(["a", "b"], [], lambda s: event.set()))
        assert self._run_status(staging, scan_id) == "interrupted"  # resumable, not failed (#86)
        assert staging.get_scan_checkpoint()["state"] == "interrupted"
        assert scanner.scan_state() == "interrupted"

    def test_incompatible_checkpoint_is_reset_with_a_warning(self, staging, caplog) -> None:
        scanner = self._make_scanner(staging, max_jobs_per_slug=3)
        self._run(scanner, _slug_walker(["a", "b"], [], lambda s: scanner.request_pause()))

        changed = self._make_scanner(staging, max_jobs_per_slug=10)
        seen: list[str] = []
        with caplog.at_level("WARNING", logger="src.hellowork.scanner"):
            self._run(changed, _slug_walker(["a", "b"], seen))
        assert seen == ["a", "b"]  # "a" re-collected under the new per-slug cap
        assert any("different configuration" in r.getMessage() for r in caplog.records)

    def test_pausing_hellowork_does_not_pause_linkedin(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import LinkedInConfig, LinkedInScanner
        from src.linkedin.staging import LinkedInStagingStorage

        li_staging = LinkedInStagingStorage(db_path=str(staging._db_path))
        li_staging.init_db()
        shared = threading.Event()  # the backend's shared shutdown event
        li = LinkedInScanner(
            config=LinkedInConfig(enabled=True), staging=li_staging,
            config_dir=str(tmp_path), shutdown_event=shared,
        )
        hw = self._make_scanner(staging, shutdown_event=shared)
        self._run(hw, _slug_walker(["a", "b"], [], lambda s: hw.request_pause()))

        assert hw.scan_state() == "paused"
        assert li.scan_state() == "idle"
        assert li_staging.get_scan_checkpoint() is None
        assert shared.is_set() is False
        li_staging.close()


# ---------------------------------------------------------------------------
# Pause/resume review follow-ups (issue #81)
# ---------------------------------------------------------------------------

class TestPauseReviewFollowUps:
    _make_scanner = TestManualPause._make_scanner
    _run = staticmethod(TestManualPause._run)
    _run_status = staticmethod(TestManualPause._run_status)

    @staticmethod
    def _failing_state_write(staging, *failing_states: str):
        import sqlite3

        real = staging.set_checkpoint_state

        def flaky(scan_id, state, create_if_missing=True):
            if state in failing_states:
                raise sqlite3.OperationalError("database is locked")
            return real(scan_id, state, create_if_missing)

        return patch.object(staging, "set_checkpoint_state", side_effect=flaky)

    def test_failed_pause_persistence_is_not_reported_as_a_pause(self, staging) -> None:
        scanner = self._make_scanner(staging)
        with self._failing_state_write(staging, "paused"):
            scan_id = self._run(
                scanner, _slug_walker(["a", "b"], [], lambda s: scanner.request_pause())
            )

        assert scanner.last_scan_paused() is False
        row = staging._get_conn().execute(
            "SELECT status, error FROM hellowork_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "pause requested but could not be persisted" in row["error"].lower()
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "failed"
        assert json.loads(cp["completed_slugs_json"]) == ["a"]  # progress kept
        assert scanner.is_paused() is False
        assert scanner.interrupted_checkpoint_id() is None

    def test_failed_pause_and_failed_label_leave_a_non_resumable_running_row(self, staging) -> None:
        scanner = self._make_scanner(staging)
        with self._failing_state_write(staging, "paused", "failed"):
            self._run(scanner, _slug_walker(["a", "b"], [], lambda s: scanner.request_pause()))
        assert staging.get_scan_checkpoint()["state"] == "running"
        assert scanner.interrupted_checkpoint_id() is None
        staging.recover_stale_scans()
        assert scanner.interrupted_checkpoint_id() is not None

    def test_scan_error_labels_the_checkpoint_failed(self, staging) -> None:
        import sqlite3

        scanner = self._make_scanner(staging)
        with patch.object(
            staging, "complete_scan_run", side_effect=sqlite3.OperationalError("database is locked")
        ):
            scan_id = self._run(scanner, _slug_walker(["a", "b"], []))
        assert self._run_status(staging, scan_id) == "failed"
        assert staging.get_scan_checkpoint()["state"] == "failed"
        assert scanner.scan_state() == "idle"

    def test_pause_mid_slug_through_the_real_collector_refetches_that_slug(self, staging) -> None:
        """End to end through HelloWorkCollector (HTTP faked at
        _fetch_with_retry, as in tests/test_hellowork_collector.py): a pause
        between two detail pages of a slug stops before the next request,
        does not check the slug off, and the resume re-collects it."""
        from tests.test_hellowork_collector import _detail_html, _html_response, _listing_html

        scanner = self._make_scanner(staging, metier_slugs=["dev-a", "dev-b"])
        url_1 = "https://www.hellowork.com/fr-fr/emplois/101.html"
        url_2 = "https://www.hellowork.com/fr-fr/emplois/102.html"
        fetched: list[str] = []

        def fetch_first(self_collector, url, *args, **kwargs):
            fetched.append(url)
            if "metier_dev-a" in url:
                return _html_response(_listing_html([url_1, url_2]))
            if url == url_1:
                scanner.request_pause()  # arrives between two detail requests
                return _html_response(_detail_html(title="Dev 101"))
            raise AssertionError(f"fetched {url} after the pause")

        with patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit"), \
             patch("src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
                   autospec=True, side_effect=fetch_first):
            scan_id = scanner.run_scan()

        assert self._run_status(staging, scan_id) == "paused"
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert json.loads(cp["completed_slugs_json"]) == []  # dev-a not checked off
        assert url_2 not in fetched

        refetched: list[str] = []

        def fetch_resume(self_collector, url, *args, **kwargs):
            refetched.append(url)
            if "metier_dev-a" in url:
                return _html_response(_listing_html([url_1, url_2]))
            if "metier_dev-b" in url:
                return _html_response(_listing_html([]))
            return _html_response(_detail_html(title="Dev " + url[-8:-5]))

        with patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit"), \
             patch("src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
                   autospec=True, side_effect=fetch_resume):
            resumed_id = scanner.run_scan()

        assert self._run_status(staging, resumed_id) == "completed"
        assert any("metier_dev-a" in u for u in refetched)  # the slug is re-collected
        assert url_2 in refetched
        assert staging.get_scan_checkpoint() is None
