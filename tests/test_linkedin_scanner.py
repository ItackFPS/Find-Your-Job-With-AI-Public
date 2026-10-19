"""Tests for LinkedInConfig, LinkedInScanner, and LinkedInScheduler.

JobSpy is mocked throughout — no live network calls are made.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def staging(tmp_db: str):
    from src.linkedin.staging import LinkedInStagingStorage
    store = LinkedInStagingStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


@pytest.fixture
def default_config():
    from src.linkedin.scanner import LinkedInConfig
    return LinkedInConfig(enabled=True)


@pytest.fixture
def config_with_queries():
    from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig
    return LinkedInConfig(
        enabled=True,
        rate_limit_seconds=0.0,  # no delay in tests
        fetch_description=False,
        queries=[
            LinkedInQueryConfig(search_term="software engineer", location="France", results_wanted=5),
        ],
    )


@pytest.fixture
def scanner(config_with_queries, staging, tmp_path):
    from src.linkedin.scanner import LinkedInScanner
    # tmp_path has no profile.yaml/filters.yaml → filter loading fails gracefully (skipped)
    return LinkedInScanner(config=config_with_queries, staging=staging, config_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# LinkedInConfig
# ---------------------------------------------------------------------------

class TestLinkedInConfig:
    def test_disabled_by_default(self) -> None:
        from src.linkedin.scanner import LinkedInConfig
        cfg = LinkedInConfig()
        assert cfg.enabled is False

    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        from src.linkedin.scanner import LinkedInConfig
        cfg = LinkedInConfig.load(str(tmp_path))
        assert cfg.enabled is False
        assert cfg.schedule_hour == 8
        assert cfg.queries == []

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        from src.linkedin.scanner import LinkedInConfig
        (tmp_path / "linkedin.yaml").write_text(
            "enabled: true\n"
            "schedule:\n"
            "  hour: 6\n"
            "  minute: 30\n"
            "rate_limit_seconds: 20.0\n"
            "queries:\n"
            "  - search_term: 'data engineer'\n"
            "    location: 'Paris'\n"
            "    results_wanted: 25\n"
        )
        cfg = LinkedInConfig.load(str(tmp_path))
        assert cfg.enabled is True
        assert cfg.schedule_hour == 6
        assert cfg.schedule_minute == 30
        assert cfg.rate_limit_seconds == 20.0
        assert len(cfg.queries) == 1
        assert cfg.queries[0].search_term == "data engineer"
        assert cfg.queries[0].results_wanted == 25

    def test_load_skips_queries_without_search_term(self, tmp_path: Path) -> None:
        from src.linkedin.scanner import LinkedInConfig
        (tmp_path / "linkedin.yaml").write_text(
            "enabled: true\n"
            "queries:\n"
            "  - location: 'France'\n"  # no search_term
        )
        cfg = LinkedInConfig.load(str(tmp_path))
        assert cfg.queries == []

    def test_load_disabled_disables_scanner(self, tmp_path: Path) -> None:
        from src.linkedin.scanner import LinkedInConfig
        (tmp_path / "linkedin.yaml").write_text("enabled: false\n")
        cfg = LinkedInConfig.load(str(tmp_path))
        assert cfg.enabled is False

    def test_default_max_pages_per_query(self) -> None:
        from src.linkedin.scanner import LinkedInConfig
        cfg = LinkedInConfig()
        assert cfg.max_pages_per_query == 50

    def test_load_max_pages_from_yaml(self, tmp_path: Path) -> None:
        from src.linkedin.scanner import LinkedInConfig
        (tmp_path / "linkedin.yaml").write_text(
            "enabled: true\n"
            "max_pages_per_query: 10\n"
        )
        cfg = LinkedInConfig.load(str(tmp_path))
        assert cfg.max_pages_per_query == 10


# ---------------------------------------------------------------------------
# LinkedInScanner — basic behaviour
# ---------------------------------------------------------------------------

class TestLinkedInScannerBasic:
    def test_is_scanning_false_initially(self, scanner) -> None:
        assert scanner.is_scanning() is False

    def test_run_scan_with_no_queries_completes(self, default_config, staging) -> None:
        from src.linkedin.scanner import LinkedInScanner
        s = LinkedInScanner(config=default_config, staging=staging)
        scan_id = s.run_scan()
        assert scan_id is not None
        row = staging._get_conn().execute(
            "SELECT status, jobs_found FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["jobs_found"] == 0

    def test_run_scan_fails_gracefully_when_jobspy_missing(self, config_with_queries, staging, tmp_path) -> None:
        """If jobspy is absent, the scan fails gracefully without crashing the app."""
        from src.linkedin.scanner import LinkedInScanner
        s = LinkedInScanner(config=config_with_queries, staging=staging, config_dir=str(tmp_path))
        # Setting sys.modules["jobspy"] = None inhibits the import even if installed
        with patch.dict("sys.modules", {"jobspy": None}):
            scan_id = s.run_scan()
        assert scan_id is not None
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "failed"

    def test_prevents_concurrent_scans(self, scanner) -> None:
        """start_scan raises RuntimeError if a scan is already in progress."""
        barrier = threading.Barrier(2)
        scan_started = threading.Event()

        def slow_scrape(**kwargs: Any) -> MagicMock:
            scan_started.set()
            barrier.wait(timeout=3)
            df = MagicMock()
            df.empty = True
            return df

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = slow_scrape

        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            # start_scan spawns a daemon thread; becomes 'scanning' immediately
            scanner.start_scan()
            scan_started.wait(timeout=2)
            assert scanner.is_scanning()

            import pytest as _pytest
            with _pytest.raises(RuntimeError, match="already in progress"):
                scanner.start_scan()

            barrier.wait(timeout=3)  # unblock the first scan

        time.sleep(0.15)  # let the thread finish
        assert not scanner.is_scanning()

    def test_is_scanning_false_after_run_completes(self, scanner) -> None:
        df_mock = MagicMock()
        df_mock.empty = True
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df_mock)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()
        assert scan_id is not None
        assert scanner.is_scanning() is False


# ---------------------------------------------------------------------------
# LinkedInScanner — crash safety on the first DB write of a scan
#
# Regression coverage for: a transient "database is locked" on
# create_scan_run() (the very first DB write _do_scan() makes) used to
# propagate straight out of the background thread, uncaught. That silently
# released the scan lock without ever recording the scan run, so the next
# trigger (scheduler tick or a manual "Scan Now") started a brand new scan_id
# from scratch — indistinguishable from the scan having reset to the
# beginning. See src/linkedin/scanner.py::_do_scan.
# ---------------------------------------------------------------------------

class TestLinkedInScannerCrashSafety:
    def test_run_scan_survives_lock_error_on_create_scan_run(self, scanner, staging) -> None:
        import sqlite3

        def raise_locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(staging, "create_scan_run", side_effect=raise_locked):
            # Must not raise — a background thread would otherwise die silently.
            scan_id = scanner.run_scan()

        assert scan_id is not None
        # The scan lock must be released so the next trigger can proceed —
        # not "stuck scanning" forever because of the failed first write.
        assert scanner.is_scanning() is False

    def test_scan_can_retry_cleanly_after_a_create_scan_run_failure(self, scanner, staging) -> None:
        """A transient failure on the first scan must not prevent the very
        next scan from running normally to completion (proves the lock
        release + retry path is clean, not just "doesn't crash")."""
        import sqlite3

        def raise_locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(staging, "create_scan_run", side_effect=raise_locked):
            scanner.run_scan()
        assert scanner.is_scanning() is False

        df_mock = MagicMock()
        df_mock.empty = True
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df_mock)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        assert scan_id is not None
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# LinkedInScanner — job persistence
# ---------------------------------------------------------------------------

def _make_df_row(
    job_id: str = "9999999999",
    title: str = "Software Engineer",
    company: str = "Acme",
    location: str = "Paris, France",
    description: str | None = "Great role",
    is_remote: bool = False,
) -> MagicMock:
    row = MagicMock()
    row.get = lambda k, default=None: {
        "id": job_id,
        "job_url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "is_remote": is_remote,
        "job_type": "fulltime",
        "date_posted": None,
        "min_amount": None,
        "max_amount": None,
        "currency": "EUR",
        "job_level": None,
        "company_url": None,
        "company_logo": None,
        "site": "linkedin",
        "interval": None,
    }.get(k, default)
    return row


def _make_mock_df(rows: list[MagicMock]) -> MagicMock:
    df = MagicMock()
    df.empty = len(rows) == 0
    df.__len__ = lambda self: len(rows)
    df.iterrows = lambda: enumerate(rows)
    return df


class TestLinkedInScannerPersistence:
    def test_scan_inserts_jobs_to_staging(self, scanner, staging) -> None:
        row = _make_df_row("1111111111")
        df = _make_mock_df([row])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1

        stored = staging._get_conn().execute(
            "SELECT * FROM linkedin_staging WHERE linkedin_job_id = '1111111111'"
        ).fetchone()
        assert stored["title"] == "Software Engineer"
        assert stored["status"] == "waiting"
        assert stored["company"] == "Acme"

    def test_scan_uses_stable_id(self, scanner, staging) -> None:
        row = _make_df_row("2222222222")
        df = _make_mock_df([row])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        expected_id = hashlib.sha256(b"linkedin:2222222222").hexdigest()[:32]
        stored = staging._get_conn().execute(
            "SELECT id FROM linkedin_staging WHERE linkedin_job_id = '2222222222'"
        ).fetchone()
        assert stored["id"] == expected_id

    def test_rescan_does_not_duplicate(self, scanner, staging) -> None:
        row = _make_df_row("3333333333")
        df = _make_mock_df([row])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()
            scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1

    def test_scan_marks_completed_in_db(self, scanner, staging) -> None:
        df = _make_mock_df([_make_df_row("4444444444")])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status, jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?",
            (scan_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["jobs_found"] == 1
        assert row["jobs_new"] == 1

    def test_scan_query_failure_is_non_fatal(self, scanner, staging) -> None:
        """Per-query exceptions are logged and skipped; the scan still completes."""
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(side_effect=RuntimeError("Connection timeout"))
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status, jobs_found FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["jobs_found"] == 0

    def test_query_failure_does_not_abort_remaining_queries(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,  # one page per query so call_count stays deterministic
            queries=[
                LinkedInQueryConfig(search_term="engineer", location="France"),
                LinkedInQueryConfig(search_term="scientist", location="France"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        call_count = 0

        def flaky_scrape(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Rate limited")
            return _make_mock_df([_make_df_row("5555555555")])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = flaky_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            s.run_scan()

        assert call_count == 2  # both queries attempted
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1  # second query's job was persisted

    def test_empty_results_are_handled_gracefully(self, scanner, staging) -> None:
        df = _make_mock_df([])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT jobs_found FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["jobs_found"] == 0

    def test_job_without_id_is_skipped(self, scanner, staging) -> None:
        row = _make_df_row(job_id="")
        df = _make_mock_df([row])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# Integration: snapshot → pipeline conversion
# ---------------------------------------------------------------------------

class TestSnapshotIntegration:
    def test_full_cycle_insert_snapshot_convert(self, scanner, staging) -> None:
        from src.linkedin.staging import staging_row_to_job

        df = _make_mock_df([_make_df_row("8888888888", title="ML Engineer", company="DeepMind")])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        # Snapshot
        rows = staging.snapshot_waiting("pipeline-run-001")
        assert len(rows) == 1

        # Convert to Job
        job = staging_row_to_job(rows[0])
        assert job.source == "linkedin"
        assert job.title == "ML Engineer"
        assert job.company == "DeepMind"

        # Mark transferred
        staging.mark_transferred("pipeline-run-001")

        # Job must still exist in staging (never deleted)
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE linkedin_job_id = '8888888888'"
        ).fetchone()[0]
        assert count == 1

        # Waiting count must be 0
        waiting = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE status = 'waiting'"
        ).fetchone()[0]
        assert waiting == 0

    def test_pipeline_failure_restores_waiting(self, scanner, staging) -> None:
        df = _make_mock_df([_make_df_row("7777777777")])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        staging.snapshot_waiting("pipeline-run-001")
        staging.reset_snapshot("pipeline-run-001")  # simulate pipeline failure

        # Job must be available for next pipeline run
        rows = staging.snapshot_waiting("pipeline-run-002")
        assert len(rows) == 1
        assert rows[0]["linkedin_job_id"] == "7777777777"


# ---------------------------------------------------------------------------
# Pre-staging hard filter
# ---------------------------------------------------------------------------

def _make_filter_module(filter_fn=None):
    """Return a mock src.pipeline.filter module for filter tests.

    Injecting it into sys.modules triggers the sys.modules guard in
    _load_filters_config(), letting filter behaviour be tested without
    triggering the 25-second SentenceTransformer cold-start from
    src.pipeline.__init__.py.
    """
    mock = MagicMock()
    mock.load_filter_config.return_value = {"_test": True}
    if filter_fn is not None:
        mock.filter_jobs.side_effect = filter_fn
    else:
        mock.filter_jobs.side_effect = lambda jobs, cfg: (jobs, {})
    return mock


class TestLinkedInScannerFilter:
    """Jobs must pass the pipeline hard filter before being staged."""

    def test_rejected_jobs_not_staged(self, scanner, staging) -> None:
        """Jobs that fail the filter are not inserted into staging."""
        row = _make_df_row("f000000001")
        df = _make_mock_df([row])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)

        def always_reject(jobs, cfg):
            return [], {j.id: "test_reject" for j in jobs}

        filter_mod = _make_filter_module(always_reject)
        with patch.dict("sys.modules", {"src.pipeline.filter": filter_mod, "jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 0

        row_db = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row_db["jobs_found"] == 1   # fetched from LinkedIn (unique)
        assert row_db["jobs_new"] == 0     # rejected by filter → not staged

    def test_passing_jobs_are_staged(self, scanner, staging) -> None:
        """Jobs that pass the filter are inserted into staging."""
        row = _make_df_row("f000000002")
        df = _make_mock_df([row])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)

        filter_mod = _make_filter_module()  # default: pass everything
        with patch.dict("sys.modules", {"src.pipeline.filter": filter_mod, "jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1

        row_db = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row_db["jobs_found"] == 1
        assert row_db["jobs_new"] == 1

    def test_filter_skipped_when_pipeline_not_loaded(self, scanner, staging) -> None:
        """Without src.pipeline.filter in sys.modules, filter is skipped (all jobs staged)."""
        # scanner fixture points config_dir at tmp_path with no yaml files.
        # Without src.pipeline.filter in sys.modules, _load_filters_config returns None.
        row = _make_df_row("f000000003")
        df = _make_mock_df([row])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)

        # Ensure src.pipeline.filter is NOT in sys.modules for this test
        import sys
        saved = sys.modules.pop("src.pipeline.filter", None)
        try:
            with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
                scanner.run_scan()
        finally:
            if saved is not None:
                sys.modules["src.pipeline.filter"] = saved

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1  # filter skipped → job staged

    def test_mixed_results_only_passing_staged(self, scanner, staging) -> None:
        """Only jobs that pass the filter are staged; rejected ones are not."""
        rows = [_make_df_row("f000000004"), _make_df_row("f000000005")]
        df = _make_mock_df(rows)
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)

        call_count = 0

        def alternating_filter(jobs, cfg):
            nonlocal call_count
            result = jobs if call_count % 2 == 0 else []
            rejections = {} if call_count % 2 == 0 else {j.id: "rejected" for j in jobs}
            call_count += 1
            return result, rejections

        filter_mod = _make_filter_module(alternating_filter)
        with patch.dict("sys.modules", {"src.pipeline.filter": filter_mod, "jobspy": mock_jobspy}):
            scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1  # one passed, one rejected


# ---------------------------------------------------------------------------
# Continuous scanning / pagination
# ---------------------------------------------------------------------------

class TestLinkedInScannerPagination:
    """Scanner pagination stops only on empty results or the safety limit."""

    def _make_scanner(self, staging, tmp_path, **cfg_kwargs):
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            **cfg_kwargs,
            queries=[LinkedInQueryConfig(search_term="engineer")],
        )
        return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

    def test_fetches_multiple_pages_when_new_jobs_found(self, staging, tmp_path) -> None:
        """Scanner requests subsequent pages as long as they are non-empty."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=5, max_results_per_query=2)

        call_args: list[dict] = []
        pages = [
            _make_mock_df([_make_df_row("p1aaaa"), _make_df_row("p1bbbb")]),  # page 0: 2 new
            _make_mock_df([_make_df_row("p1cccc"), _make_df_row("p1dddd")]),  # page 1: 2 new
            _make_mock_df([]),                                                  # page 2: empty → stop
        ]

        def paged_scrape(**kwargs):
            call_args.append(kwargs)
            return pages[min(len(call_args) - 1, len(pages) - 1)]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        assert len(call_args) == 3  # pages 0, 1, 2
        assert call_args[0]["offset"] == 0
        assert call_args[1]["offset"] == 2
        assert call_args[2]["offset"] == 4
        # python-jobspy >= 1.3 name; the deprecated linkedin_fetch_description would land in **kwargs
        # unread once removed, leaving every description empty without an error.
        assert all("fetch_description" in c and "linkedin_fetch_description" not in c for c in call_args)

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 4

        row = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["jobs_found"] == 4
        assert row["jobs_new"] == 4

    def test_pagination_stops_on_empty_results(self, staging, tmp_path) -> None:
        """Pagination stops when JobSpy returns an empty DataFrame."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=10, max_results_per_query=2)

        call_count = 0
        pages = [
            _make_mock_df([_make_df_row("stop0001"), _make_df_row("stop0002")]),  # page 0
            _make_mock_df([]),  # page 1: empty → stop
        ]

        def paged_scrape(**kwargs):
            nonlocal call_count
            result = pages[min(call_count, len(pages) - 1)]
            call_count += 1
            return result

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        assert call_count == 2  # stopped after empty page 1

    def test_pagination_continues_when_page_has_only_duplicates(
        self, staging, tmp_path
    ) -> None:
        """Pagination must NOT stop when a page returns only jobs already in scan_seen_ids."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=5, max_results_per_query=2)

        call_count = 0
        pages = [
            _make_mock_df([_make_df_row("dup0001"), _make_df_row("dup0002")]),  # page 0: 2 new
            _make_mock_df([_make_df_row("dup0001"), _make_df_row("dup0002")]),  # page 1: same 2 (0 unique) — must NOT stop
            _make_mock_df([_make_df_row("dup0003")]),                            # page 2: 1 new
            _make_mock_df([]),                                                   # page 3: empty → stop
        ]

        def paged_scrape(**kwargs):
            nonlocal call_count
            idx = min(call_count, len(pages) - 1)
            call_count += 1
            return pages[idx]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        # Pages 0, 1, 2, 3 all fetched — not stopped early on page 1
        assert call_count == 4

        # Only 3 unique jobs (dup0001, dup0002, dup0003)
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 3

        row = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["jobs_found"] == 3
        assert row["jobs_new"] == 3

    def test_pagination_continues_when_page_has_only_filtered_jobs(
        self, staging, tmp_path
    ) -> None:
        """Pagination must NOT stop when all jobs on a page are rejected by the hard filter."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=5, max_results_per_query=2)

        call_count = 0
        pages = [
            _make_mock_df([_make_df_row("filt0001"), _make_df_row("filt0002")]),  # page 0: both rejected
            _make_mock_df([_make_df_row("filt0003"), _make_df_row("filt0004")]),  # page 1: both accepted
            _make_mock_df([]),                                                     # page 2: empty → stop
        ]

        def paged_scrape(**kwargs):
            nonlocal call_count
            idx = min(call_count, len(pages) - 1)
            call_count += 1
            return pages[idx]

        rejected_ids: set[str] = {"filt0001", "filt0002"}

        def selective_filter(jobs, cfg):
            passed = [j for j in jobs if j.source_job_id not in rejected_ids]
            rejected = {j.id: "loc_filter" for j in jobs if j.source_job_id in rejected_ids}
            return passed, rejected

        filter_mod = _make_filter_module(selective_filter)
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape

        with patch.dict("sys.modules", {"src.pipeline.filter": filter_mod, "jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        # All 3 pages fetched — not stopped early after the all-rejected page 0
        assert call_count == 3

        # Only page 1 jobs staged
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 2

        row = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["jobs_found"] == 4   # 4 unique fetched
        assert row["jobs_new"] == 2     # 2 passed filter and were new

    def test_dedup_within_scan_query(self, staging, tmp_path) -> None:
        """Same linkedin_job_id appearing on multiple pages is only counted once in jobs_found."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=3, max_results_per_query=2)

        call_count = 0
        # Each page returns the same job; stop via empty page at the end
        pages = [
            _make_mock_df([_make_df_row("d1aaaa"), _make_df_row("d1aaaa")]),  # page 0
            _make_mock_df([_make_df_row("d1aaaa"), _make_df_row("d1aaaa")]),  # page 1: all dupes
            _make_mock_df([]),                                                  # page 2: empty → stop
        ]

        def paged_scrape(**kwargs):
            nonlocal call_count
            idx = min(call_count, len(pages) - 1)
            call_count += 1
            return pages[idx]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1  # inserted once despite appearing on multiple pages

        row = staging._get_conn().execute(
            "SELECT jobs_found FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["jobs_found"] == 1  # counted once via scan_seen_ids dedup

    def test_safety_limit_max_pages(self, staging, tmp_path) -> None:
        """Scanner stops after max_pages_per_query even if every page has new jobs."""
        max_pages = 3
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=max_pages, max_results_per_query=1)

        call_count = 0

        def infinite_scrape(**kwargs):
            nonlocal call_count
            call_count += 1
            # Each call returns a brand new job id so it always looks new
            return _make_mock_df([_make_df_row(f"lim{call_count:06d}")])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = infinite_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        assert call_count == max_pages  # capped at max_pages_per_query

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == max_pages

    def test_dedup_across_pages_does_not_break_between_query_scans(
        self, staging, tmp_path
    ) -> None:
        """scan_seen_ids resets between separate scan runs (not shared across scans)."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=1, max_results_per_query=2)

        df = _make_mock_df([_make_df_row("cross1")])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan1 = scanner.run_scan()
            scan2 = scanner.run_scan()

        # Second scan: job already in staging → insert_job returns False → jobs_new = 0
        row1 = staging._get_conn().execute(
            "SELECT jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan1,)
        ).fetchone()
        row2 = staging._get_conn().execute(
            "SELECT jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan2,)
        ).fetchone()
        assert row1["jobs_new"] == 1
        assert row2["jobs_new"] == 0


# ---------------------------------------------------------------------------
# Cross-query deduplication
# ---------------------------------------------------------------------------

class TestCrossQueryDedup:
    """scan_seen_ids is shared across queries — a job fetched by query A is not
    counted again when query B returns the same job."""

    def test_same_job_in_two_queries_counted_once(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            max_results_per_query=5,
            queries=[
                LinkedInQueryConfig(search_term="engineer"),
                LinkedInQueryConfig(search_term="developer"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        # Both queries return the same two jobs
        shared_job_a = _make_df_row("shared0001")
        shared_job_b = _make_df_row("shared0002")
        df = _make_mock_df([shared_job_a, shared_job_b])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        # Only 2 unique jobs in staging, not 4
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 2

        row = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        # jobs_found = unique (2), not raw (4)
        assert row["jobs_found"] == 2
        assert row["jobs_new"] == 2

    def test_unique_jobs_in_each_query_all_staged(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            max_results_per_query=5,
            queries=[
                LinkedInQueryConfig(search_term="engineer"),
                LinkedInQueryConfig(search_term="developer"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        call_count = 0

        def query_specific_scrape(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_mock_df([_make_df_row("q1job0001"), _make_df_row("q1job0002")])
            return _make_mock_df([_make_df_row("q2job0001"), _make_df_row("q2job0002")])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = query_specific_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        # 4 distinct jobs total
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 4

        row = staging._get_conn().execute(
            "SELECT jobs_found, jobs_new FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["jobs_found"] == 4
        assert row["jobs_new"] == 4


# ---------------------------------------------------------------------------
# Statistics accuracy
# ---------------------------------------------------------------------------

class TestScanStatistics:
    """Verify that the stats_json stored in the DB correctly reflects what happened."""

    def test_stats_json_stored_on_completion(self, staging, tmp_path) -> None:
        import json as _json

        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            max_results_per_query=5,
            queries=[LinkedInQueryConfig(search_term="engineer")],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        df = _make_mock_df([_make_df_row("s001"), _make_df_row("s002")])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        # Force a second page to return empty (max_pages=1 so only 1 call)
        row = staging._get_conn().execute(
            "SELECT stats_json FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        stats = _json.loads(row["stats_json"])

        assert stats["total_unique"] == 2
        assert stats["total_staged"] == 2
        assert stats["total_cross_query_dups"] == 0
        assert stats["total_filtered_out"] == 0
        assert stats["total_already_known"] == 0
        assert stats["queries_completed"] == 1
        assert len(stats["queries"]) == 1
        q = stats["queries"][0]
        assert q["search_term"] == "engineer"
        assert q["jobs_unique"] == 2
        assert q["jobs_staged"] == 2
        assert q["jobs_cross_query_dups"] == 0
        assert q["jobs_unique_to_query"] == 2
        # stopped_by safety_limit because max_pages=1 and only 1 non-empty page was returned
        # (the loop hit its limit without a break)
        assert q["stopped_by"] == "safety_limit"

    def test_stats_distinguish_filtered_and_known(self, staging, tmp_path) -> None:
        import json as _json

        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=2,
            max_results_per_query=5,
            queries=[LinkedInQueryConfig(search_term="engineer")],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        call_count = 0
        pages = [
            _make_mock_df([_make_df_row("st001"), _make_df_row("st002"), _make_df_row("st003")]),
            _make_mock_df([]),
        ]

        def paged_scrape(**kwargs):
            nonlocal call_count
            result = pages[min(call_count, len(pages) - 1)]
            call_count += 1
            return result

        # Reject st001, accept st002 and st003
        def partial_filter(jobs, cfg):
            passed = [j for j in jobs if j.source_job_id != "st001"]
            rejected = {j.id: "test" for j in jobs if j.source_job_id == "st001"}
            return passed, rejected

        filter_mod = _make_filter_module(partial_filter)
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape

        with patch.dict("sys.modules", {"src.pipeline.filter": filter_mod, "jobspy": mock_jobspy}):
            # First scan: st001 filtered, st002 and st003 staged
            s.run_scan()
            call_count = 0  # reset
            # Second scan: same 3 jobs; st001 filtered, st002/st003 already known
            scan_id2 = s.run_scan()

        row2 = staging._get_conn().execute(
            "SELECT stats_json FROM linkedin_scan_runs WHERE id = ?", (scan_id2,)
        ).fetchone()
        stats2 = _json.loads(row2["stats_json"])

        assert stats2["total_unique"] == 3        # 3 unique jobs seen
        assert stats2["total_filtered_out"] == 1  # st001 rejected
        assert stats2["total_already_known"] == 2 # st002, st003 already in DB
        assert stats2["total_staged"] == 0        # nothing new

    def test_stats_query_stopped_by_exhaustion(self, staging, tmp_path) -> None:
        import json as _json

        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=10,
            max_results_per_query=5,
            queries=[LinkedInQueryConfig(search_term="engineer")],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        call_count = 0
        pages = [
            _make_mock_df([_make_df_row("ex001")]),
            _make_mock_df([]),  # page 1 empty → exhaustion
        ]

        def paged_scrape(**kwargs):
            nonlocal call_count
            idx = min(call_count, len(pages) - 1)
            call_count += 1
            return pages[idx]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        row = staging._get_conn().execute(
            "SELECT stats_json FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        stats = _json.loads(row["stats_json"])

        assert stats["queries_stopped_by_exhaustion"] == 1
        assert stats["queries_stopped_by_limit"] == 0
        assert stats["queries"][0]["stopped_by"] == "exhaustion"

    def test_stats_query_failed_reported(self, staging, tmp_path) -> None:
        import json as _json

        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            queries=[
                LinkedInQueryConfig(search_term="engineer"),
                LinkedInQueryConfig(search_term="scientist"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        call_count = 0

        def flaky(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("429")
            return _make_mock_df([_make_df_row("ok001")])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = flaky
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        row = staging._get_conn().execute(
            "SELECT stats_json FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        stats = _json.loads(row["stats_json"])

        assert stats["queries_failed"] == 1
        assert stats["queries_completed"] == 2  # both attempted, one failed
        # failed query has stopped_by="error"
        failed = [q for q in stats["queries"] if q["stopped_by"] == "error"]
        assert len(failed) == 1
        assert failed[0]["search_term"] == "engineer"


# ---------------------------------------------------------------------------
# Query attribution
# ---------------------------------------------------------------------------

class TestQueryAttribution:
    """discovered_by_query / seen_by_queries / jobs_cross_query_dups / jobs_unique_to_query."""

    def test_discovered_by_query_set_on_staged_job(self, staging, tmp_path) -> None:
        import json as _json
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            queries=[LinkedInQueryConfig(search_term="python developer")],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))
        df = _make_mock_df([_make_df_row("qa0001"), _make_df_row("qa0002")])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            s.run_scan()

        rows = staging._get_conn().execute(
            "SELECT linkedin_job_id, discovered_by_query, seen_by_queries FROM linkedin_staging"
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["discovered_by_query"] == "python developer"
            assert _json.loads(row["seen_by_queries"]) == ["python developer"]

    def test_cross_query_dup_updates_seen_by_queries(self, staging, tmp_path) -> None:
        import json as _json
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            queries=[
                LinkedInQueryConfig(search_term="python developer"),
                LinkedInQueryConfig(search_term="backend developer"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        # qa0003 appears in both queries; qa0004 only in the first
        shared_row = _make_df_row("qa0003")
        unique_row = _make_df_row("qa0004")
        second_query_unique = _make_df_row("qa0005")

        call_count = [0]
        pages = [
            _make_mock_df([shared_row, unique_row]),      # python developer
            _make_mock_df([shared_row, second_query_unique]),  # backend developer
        ]

        def paged(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return pages[min(idx, len(pages) - 1)]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            s.run_scan()

        # qa0003 discovered by "python developer", also seen by "backend developer"
        row = staging._get_conn().execute(
            "SELECT discovered_by_query, seen_by_queries FROM linkedin_staging "
            "WHERE linkedin_job_id = 'qa0003'"
        ).fetchone()
        assert row["discovered_by_query"] == "python developer"
        seen = _json.loads(row["seen_by_queries"])
        assert "python developer" in seen
        assert "backend developer" in seen

        # qa0004 unique to "python developer"
        row4 = staging._get_conn().execute(
            "SELECT discovered_by_query, seen_by_queries FROM linkedin_staging "
            "WHERE linkedin_job_id = 'qa0004'"
        ).fetchone()
        assert row4["discovered_by_query"] == "python developer"
        assert _json.loads(row4["seen_by_queries"]) == ["python developer"]

        # qa0005 unique to "backend developer"
        row5 = staging._get_conn().execute(
            "SELECT discovered_by_query, seen_by_queries FROM linkedin_staging "
            "WHERE linkedin_job_id = 'qa0005'"
        ).fetchone()
        assert row5["discovered_by_query"] == "backend developer"
        assert _json.loads(row5["seen_by_queries"]) == ["backend developer"]

    def test_cross_query_dups_counted_in_stats(self, staging, tmp_path) -> None:
        import json as _json
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            queries=[
                LinkedInQueryConfig(search_term="python developer"),
                LinkedInQueryConfig(search_term="backend developer"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        shared = _make_df_row("qa0010")
        unique_q1 = _make_df_row("qa0011")
        unique_q2 = _make_df_row("qa0012")

        call_count = [0]
        pages = [
            _make_mock_df([shared, unique_q1]),
            _make_mock_df([shared, unique_q2]),
        ]

        def paged(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return pages[min(idx, len(pages) - 1)]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        row = staging._get_conn().execute(
            "SELECT stats_json FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        stats = _json.loads(row["stats_json"])

        assert stats["total_cross_query_dups"] == 1  # shared seen twice

        q_python = next(q for q in stats["queries"] if q["search_term"] == "python developer")
        q_backend = next(q for q in stats["queries"] if q["search_term"] == "backend developer")

        assert q_python["jobs_cross_query_dups"] == 0
        assert q_python["jobs_unique_to_query"] == 1   # unique_q1

        assert q_backend["jobs_cross_query_dups"] == 1  # shared already seen
        assert q_backend["jobs_unique_to_query"] == 1   # unique_q2

    def test_no_cross_query_dups_when_queries_disjoint(self, staging, tmp_path) -> None:
        import json as _json
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=1,
            queries=[
                LinkedInQueryConfig(search_term="python developer"),
                LinkedInQueryConfig(search_term="backend developer"),
            ],
        )
        s = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        call_count = [0]
        pages = [
            _make_mock_df([_make_df_row("qa0020"), _make_df_row("qa0021")]),
            _make_mock_df([_make_df_row("qa0022"), _make_df_row("qa0023")]),
        ]

        def paged(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return pages[min(idx, len(pages) - 1)]

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = paged
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = s.run_scan()

        row = staging._get_conn().execute(
            "SELECT stats_json FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        stats = _json.loads(row["stats_json"])

        assert stats["total_cross_query_dups"] == 0
        for q in stats["queries"]:
            assert q["jobs_cross_query_dups"] == 0
            assert q["jobs_unique_to_query"] == 2


# ---------------------------------------------------------------------------
# France attribution — query_location fallback
# ---------------------------------------------------------------------------

def _make_df_row_with_location(
    job_id: str,
    location: str | None,
    is_remote: bool = False,
) -> MagicMock:
    """Create a mock DataFrame row with a specific location."""
    row = MagicMock()
    row.get = lambda k, default=None: {
        "id": job_id,
        "job_url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "title": "Software Engineer",
        "company": "Acme",
        "location": location,
        "description": None,
        "is_remote": is_remote,
        "job_type": "fulltime",
        "date_posted": None,
        "min_amount": None,
        "max_amount": None,
        "currency": "EUR",
        "job_level": None,
        "company_url": None,
        "company_logo": None,
        "site": "linkedin",
        "interval": None,
    }.get(k, default)
    return row


def _scanner_with_location(staging, tmp_path, query_location: str):
    """Build a scanner configured with a single France query."""
    from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner
    cfg = LinkedInConfig(
        enabled=True,
        rate_limit_seconds=0.0,
        max_pages_per_query=1,
        queries=[LinkedInQueryConfig(search_term="software engineer", location=query_location)],
    )
    return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))


class TestFranceAttribution:
    """France scanner jobs must have countries=[France] regardless of location string."""

    def _scan_and_get_countries(self, staging, scanner, location: str | None, is_remote: bool = False) -> str:
        row = _make_df_row_with_location("fa000001", location=location, is_remote=is_remote)
        df = _make_mock_df([row])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()
        stored = staging._get_conn().execute(
            "SELECT countries FROM linkedin_staging WHERE linkedin_job_id = 'fa000001'"
        ).fetchone()
        staging._get_conn().execute("DELETE FROM linkedin_staging WHERE linkedin_job_id = 'fa000001'")
        staging._get_conn().execute("DELETE FROM linkedin_scan_runs")
        staging._get_conn().commit()
        return stored["countries"] if stored else "[]"

    def test_france_paris_gets_france(self, staging, tmp_path) -> None:
        """Job with location 'Paris, France' is attributed to France."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "France")
        countries_raw = self._scan_and_get_countries(staging, scanner, "Paris, France")
        assert _json.loads(countries_raw) == ["France"]

    def test_france_remote_gets_france(self, staging, tmp_path) -> None:
        """Remote job from France scanner has countries=['France'] even when location='Remote'."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "France")
        countries_raw = self._scan_and_get_countries(staging, scanner, "Remote", is_remote=True)
        assert _json.loads(countries_raw) == ["France"]

    def test_france_remote_location_string_gets_france(self, staging, tmp_path) -> None:
        """Job with location='France (Remote)' is attributed to France."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "France")
        countries_raw = self._scan_and_get_countries(staging, scanner, "France (Remote)")
        assert _json.loads(countries_raw) == ["France"]

    def test_france_lyon_gets_france(self, staging, tmp_path) -> None:
        """Job with city location 'Lyon' is attributed to France."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "France")
        countries_raw = self._scan_and_get_countries(staging, scanner, "Lyon")
        assert _json.loads(countries_raw) == ["France"]

    def test_france_null_location_gets_france(self, staging, tmp_path) -> None:
        """Job with no location (None) from France scanner gets attributed to France."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "France")
        countries_raw = self._scan_and_get_countries(staging, scanner, None)
        assert _json.loads(countries_raw) == ["France"]

    def test_germany_scanner_berlin_gets_germany(self, staging, tmp_path) -> None:
        """Germany scanner: job with 'Berlin' location is attributed to Germany."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "Germany")
        countries_raw = self._scan_and_get_countries(staging, scanner, "Berlin, Germany")
        assert _json.loads(countries_raw) == ["Germany"]

    def test_germany_scanner_remote_gets_germany(self, staging, tmp_path) -> None:
        """Germany scanner: remote job with location='Remote' gets Germany from query fallback."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "Germany")
        countries_raw = self._scan_and_get_countries(staging, scanner, "Remote", is_remote=True)
        assert _json.loads(countries_raw) == ["Germany"]

    def test_no_query_location_no_forced_country(self, staging, tmp_path) -> None:
        """Scanner with no query location does not force-assign a country to a remote job."""
        import json as _json
        scanner = _scanner_with_location(staging, tmp_path, "")
        countries_raw = self._scan_and_get_countries(staging, scanner, "Remote", is_remote=True)
        assert _json.loads(countries_raw) == []


def _make_staging_record(linkedin_job_id: str, countries_json: str = "[]") -> dict:
    """Minimal staging record for backfill / insert_job tests."""
    import hashlib as _hashlib
    from datetime import UTC, datetime as _dt
    now = _dt.now(UTC).isoformat()
    return {
        "id": _hashlib.sha256(f"linkedin:{linkedin_job_id}".encode()).hexdigest()[:32],
        "linkedin_job_id": linkedin_job_id,
        "canonical_url": f"https://www.linkedin.com/jobs/view/{linkedin_job_id}",
        "title": "Test Job",
        "company": "Acme",
        "location": "Paris, France",
        "countries": countries_json,
        "is_remote": 0,
        "work_mode": None,
        "employment_type": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "EUR",
        "description": None,
        "description_fetched": 0,
        "published_at": None,
        "raw_payload": "{}",
        "scan_run_id": "scan-test",
        "discovered_at": now,
        "last_seen_at": now,
    }


class TestFranceStagingBackfill:
    """staging.insert_job updates countries when existing record has [] and new has a country."""

    def test_countries_updated_on_rediscovery_when_was_empty(self, staging) -> None:
        """A previously empty-country record gets its country updated when re-discovered with one."""
        import json as _json

        # First insert: no country
        staging.insert_job(_make_staging_record("bf000001", countries_json="[]"))

        row = staging._get_conn().execute(
            "SELECT countries FROM linkedin_staging WHERE linkedin_job_id = 'bf000001'"
        ).fetchone()
        assert _json.loads(row["countries"]) == []

        # Re-discovery with France
        staging.insert_job(_make_staging_record("bf000001", countries_json='["France"]'))

        row2 = staging._get_conn().execute(
            "SELECT countries FROM linkedin_staging WHERE linkedin_job_id = 'bf000001'"
        ).fetchone()
        assert _json.loads(row2["countries"]) == ["France"]

    def test_existing_country_not_overwritten_on_rediscovery(self, staging) -> None:
        """A record that already has a country is not overwritten on re-discovery."""
        import json as _json

        staging.insert_job(_make_staging_record("bf000002", countries_json='["France"]'))

        # Re-discover with different country
        staging.insert_job(_make_staging_record("bf000002", countries_json='["Germany"]'))

        row = staging._get_conn().execute(
            "SELECT countries FROM linkedin_staging WHERE linkedin_job_id = 'bf000002'"
        ).fetchone()
        # Original country preserved
        assert _json.loads(row["countries"]) == ["France"]


# ---------------------------------------------------------------------------
# Stale-record rediscovery: end-to-end through run_scan()
# ---------------------------------------------------------------------------

class TestStaleRecordRediscoveryEndToEnd:
    """A controlled, fully-mocked LinkedIn scan verifying that old records
    staged with no description (from before description enrichment worked)
    are naturally rediscovered, fetch a description, and flow back through
    the normal pipeline without creating duplicates."""

    @pytest.fixture
    def enrich_scanner(self, tmp_path, staging):
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            fetch_description=False,
            enrich_descriptions=True,
            description_fetch_delay=0.0,
            queries=[LinkedInQueryConfig(search_term="software engineer", location="France", results_wanted=5)],
        )
        return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

    def test_incomplete_job_is_rediscovered_and_completed(self, enrich_scanner, staging) -> None:
        job_id = "5550001111"

        # Scan 1: simulates the old broken behaviour — JobSpy returns the job
        # with no description, and the targeted fallback fetch also fails
        # (e.g. login-gated), so it is staged with description_fetched=0.
        df1 = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df1)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch("requests.get") as mock_get_fail:
            mock_get_fail.return_value = MagicMock(status_code=200, text="<html></html>")
            enrich_scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT description, description_fetched, status FROM linkedin_staging "
            "WHERE linkedin_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["description"] is None
        assert row["description_fetched"] == 0
        assert row["status"] == "waiting"  # 1. eligible for rediscovery already

        # Simulate the old pipeline having transferred this incomplete job to
        # the jobs table before descriptions were fixed.
        staging.snapshot_waiting("old-run-001")
        staging.mark_transferred("old-run-001")
        assert staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = ?", (job_id,)
        ).fetchone()["status"] == "transferred"

        # Scan 2: LinkedIn still lists the job, and this time the guest API
        # returns a real description.
        recovered_desc = (
            "Senior Software Engineer. Fully remote position based in France. "
            "Python, Django and PostgreSQL experience required for this role."
        )
        df2 = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy2 = MagicMock()
        mock_jobspy2.scrape_jobs = MagicMock(return_value=df2)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy2}), \
             patch("requests.get") as mock_get_ok:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"<html><body>{recovered_desc}</body></html>"
            mock_get_ok.return_value = mock_resp
            enrich_scanner.run_scan()

        # 2. The rediscovered version with a description is accepted.
        row2 = staging._get_conn().execute(
            "SELECT description, description_fetched, status, snapshot_run_id "
            "FROM linkedin_staging WHERE linkedin_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row2["description"] == recovered_desc
        assert row2["description_fetched"] == 1
        assert row2["status"] == "waiting"  # reopened from 'transferred'
        assert row2["snapshot_run_id"] is None

        # 3. The old incomplete record does not cause a duplicate.
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE linkedin_job_id = ?", (job_id,)
        ).fetchone()[0]
        assert count == 1

        # 6. Future normal scans continue working: the reopened job flows
        # through the pipeline like any other waiting job.
        snapshotted = staging.snapshot_waiting("new-run-002")
        assert len(snapshotted) == 1
        assert snapshotted[0]["linkedin_job_id"] == job_id
        assert snapshotted[0]["description"] == recovered_desc

    def test_complete_job_untouched_by_later_rescans(self, enrich_scanner, staging) -> None:
        """4. A valid, already-complete LinkedIn job is never reset or refetched."""
        job_id = "5550002222"
        original_desc = "Original, already-complete job description."

        df1 = _make_mock_df([_make_df_row(job_id, description=original_desc)])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df1)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            enrich_scanner.run_scan()

        staging.snapshot_waiting("run-A")
        staging.mark_transferred("run-A")

        # Re-scan: LinkedIn still lists the job; no targeted fetch must occur.
        df2 = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy2 = MagicMock()
        mock_jobspy2.scrape_jobs = MagicMock(return_value=df2)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy2}), \
             patch("requests.get") as mock_get:
            enrich_scanner.run_scan()

        mock_get.assert_not_called()
        row = staging._get_conn().execute(
            "SELECT description, status FROM linkedin_staging WHERE linkedin_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["description"] == original_desc
        assert row["status"] == "transferred"  # untouched

    def test_other_source_jobs_in_main_table_untouched(self, tmp_path) -> None:
        """5. Jobs from other sources are never affected by LinkedIn staging changes.

        needs_description() and insert_job()'s stale-status reset only ever
        touch the linkedin_staging table, which is source-specific by
        construction — this documents that boundary explicitly.
        """
        from src.storage.sqlite import Storage
        from src.models.job import Job
        from datetime import UTC, datetime

        db_path = str(tmp_path / "jobs.db")
        db = Storage(db_path=db_path)
        db.init_db()
        other_job = Job(
            id="indeed-job-1",
            source="indeed",
            source_job_id="indeed-1",
            title="Data Analyst",
            company="Other Co",
            description=None,
            discovered_at=datetime.now(UTC),
        )
        db.upsert_job(other_job)

        # LinkedIn staging operations never touch other-source rows.
        from src.linkedin.staging import LinkedInStagingStorage
        li_staging = LinkedInStagingStorage(db_path=db_path)
        li_staging.init_db()
        assert li_staging.needs_description("indeed-1") is True  # unrelated id, unknown to LinkedIn

        row = db._get_conn().execute(
            "SELECT description FROM jobs WHERE id = 'indeed-job-1'"
        ).fetchone()
        assert row["description"] is None  # untouched by the LinkedIn staging call
        li_staging.close()


# ---------------------------------------------------------------------------
# Deleted-job classification: HTTP 404/410 marks description_fetched=2 and
# is never retried again (fetch_linkedin_description_classified's "deleted"
# status is wired through insert_job() instead of being discarded).
# ---------------------------------------------------------------------------

class TestDeletedJobClassificationWiring:
    @pytest.fixture
    def enrich_scanner(self, tmp_path, staging):
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            fetch_description=False,
            enrich_descriptions=True,
            description_fetch_delay=0.0,
            queries=[LinkedInQueryConfig(search_term="software engineer", location="France", results_wanted=5)],
        )
        return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

    def test_http_404_marks_description_fetched_2(self, enrich_scanner, staging) -> None:
        job_id = "5559990001"
        df = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch("requests.get", return_value=mock_resp):
            enrich_scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT description, description_fetched, status FROM linkedin_staging "
            "WHERE linkedin_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["description"] is None
        assert row["description_fetched"] == 2
        assert row["status"] == "waiting"

    def test_http_410_marks_description_fetched_2(self, enrich_scanner, staging) -> None:
        job_id = "5559990002"
        df = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = MagicMock(return_value=df)

        mock_resp = MagicMock()
        mock_resp.status_code = 410

        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch("requests.get", return_value=mock_resp):
            enrich_scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT description_fetched FROM linkedin_staging WHERE linkedin_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["description_fetched"] == 2

    def test_deleted_job_is_never_retried_on_rescan(self, enrich_scanner, staging) -> None:
        """Once marked description_fetched=2, needs_description() must keep
        returning False forever, so a later scan never re-issues the fetch —
        this is the actual regression the classification wiring exists to fix
        (previously the 'deleted' status was computed and immediately
        discarded, so description_fetched stayed 0 and the job was retried
        on every single rescan indefinitely)."""
        job_id = "5559990003"

        df1 = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy1 = MagicMock()
        mock_jobspy1.scrape_jobs = MagicMock(return_value=df1)
        mock_resp_404 = MagicMock()
        mock_resp_404.status_code = 404
        with patch.dict("sys.modules", {"jobspy": mock_jobspy1}), \
             patch("requests.get", return_value=mock_resp_404):
            enrich_scanner.run_scan()

        assert staging.needs_description(job_id) is False

        df2 = _make_mock_df([_make_df_row(job_id, description=None)])
        mock_jobspy2 = MagicMock()
        mock_jobspy2.scrape_jobs = MagicMock(return_value=df2)
        with patch.dict("sys.modules", {"jobspy": mock_jobspy2}), \
             patch("requests.get") as mock_get_rescan:
            enrich_scanner.run_scan()

        mock_get_rescan.assert_not_called()
        row = staging._get_conn().execute(
            "SELECT description_fetched FROM linkedin_staging WHERE linkedin_job_id = ?",
            (job_id,),
        ).fetchone()
        assert row["description_fetched"] == 2


# ---------------------------------------------------------------------------
# Resumable page/query checkpoint
#
# A genuine process crash mid-scan is simulated by calling _run_query()
# directly (bypassing _do_scan's per-query try/except, which deliberately
# catches and moves on from a query-level error — that pre-existing
# "abort this query, continue with the next" behavior is unrelated to
# process-level interruption and must not change). What actually happens
# when the process dies mid-page-loop is that nothing after the crash point
# runs — no exception handler, nothing — so calling _run_query() directly
# and letting its exception propagate uncaught is the accurate simulation.
# ---------------------------------------------------------------------------

class TestScanCheckpointResume:
    def _make_scanner(self, staging, tmp_path, **cfg_kwargs):
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            **cfg_kwargs,
            queries=[LinkedInQueryConfig(search_term="engineer", location="France", results_wanted=1)],
        )
        return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

    def test_checkpoint_persists_page_position_incrementally(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=10, max_results_per_query=1)
        query = scanner.config.queries[0]

        pages = [
            _make_mock_df([_make_df_row("ckpt0001")]),  # page 0
            _make_mock_df([_make_df_row("ckpt0002")]),  # page 1
        ]

        def crash_on_page_2(**kwargs):
            page = kwargs["offset"]  # page_size=1, so offset == page index
            if page >= 2:
                raise RuntimeError("simulated process crash mid-page-loop")
            return pages[page]

        with pytest.raises(RuntimeError, match="simulated process crash"):
            scanner._run_query("scan-crash-1", query, crash_on_page_2, None, {})

        # Only pages 0 and 1 were fully processed before the "crash" — the
        # checkpoint must reflect exactly that, not 0 and not further.
        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["current_page"] == 2
        from src.linkedin.scanner import _query_key
        assert cp["current_query_key"] == _query_key(query)

        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 2  # both pre-crash pages' jobs were already staged

    def test_resumed_scan_skips_already_fetched_pages(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=10, max_results_per_query=1)
        query = scanner.config.queries[0]

        # --- Attempt 1: crashes after pages 0 and 1 ---
        pages_attempt_1 = [
            _make_mock_df([_make_df_row("res0001")]),
            _make_mock_df([_make_df_row("res0002")]),
        ]

        def crash_on_page_2(**kwargs):
            page = kwargs["offset"]
            if page >= 2:
                raise RuntimeError("simulated crash")
            return pages_attempt_1[page]

        with pytest.raises(RuntimeError):
            scanner._run_query("scan-crash", query, crash_on_page_2, None, {})

        # --- Attempt 2: a brand new scan (the app restarted) resumes ---
        offsets_requested: list[int] = []

        def resumed_scrape(**kwargs):
            offset = kwargs["offset"]
            offsets_requested.append(offset)
            if offset == 2:
                return _make_mock_df([_make_df_row("res0003")])
            return _make_mock_df([])  # exhausted

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = resumed_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        # Pages 0 and 1 (offsets 0 and 1) were never re-requested.
        assert 0 not in offsets_requested
        assert 1 not in offsets_requested
        assert offsets_requested[0] == 2

        # All three jobs across both attempts are present, exactly once each
        # — resuming did not create duplicates and did not lose the jobs
        # staged before the crash.
        rows = staging._get_conn().execute(
            "SELECT linkedin_job_id, COUNT(*) as n FROM linkedin_staging GROUP BY linkedin_job_id"
        ).fetchall()
        counts = {r["linkedin_job_id"]: r["n"] for r in rows}
        assert counts == {"res0001": 1, "res0002": 1, "res0003": 1}

        # A full clean completion clears the checkpoint — the *next*
        # scheduled scan starts from page 0 again.
        assert staging.get_scan_checkpoint() is None
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"

    def test_completed_query_is_skipped_entirely_on_resume(self, staging, tmp_path) -> None:
        """A query whose checkpoint marks it fully completed must not be
        re-queried at all on a resumed scan (avoids duplicate processing at
        the query level, not just the page level)."""
        from src.linkedin.scanner import _query_key

        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=5, max_results_per_query=1)
        query = scanner.config.queries[0]
        staging.save_scan_checkpoint("scan-prev", [_query_key(query)], None, 0)

        mock_scrape = MagicMock()
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = mock_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        mock_scrape.assert_not_called()
        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"
        # The scan finished (every query attempted, even if by skipping) —
        # checkpoint is cleared for the next scheduled scan.
        assert staging.get_scan_checkpoint() is None

    def test_checkpoint_not_reset_by_concurrent_api_style_reads(self, staging, tmp_path) -> None:
        """Regression: reading scan status (what the dashboard polls) while
        a checkpoint exists must never reset it to page 0."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=10, max_results_per_query=1)
        query = scanner.config.queries[0]
        from src.linkedin.scanner import _query_key
        staging.save_scan_checkpoint("scan-x", [], _query_key(query), 4)

        for _ in range(20):
            staging.get_status()
            staging.list_scan_runs()
            staging.count_by_status()

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["current_page"] == 4


class TestGracefulShutdown:
    """Regression coverage for the backend-shutdown cooperation added after
    the incident where a `uvicorn --reload` restart abruptly killed the
    in-flight LinkedIn scan thread (a raw daemon thread invisible to
    uvicorn's own graceful-shutdown tracking). A shutdown_event, checked
    between pages/queries, lets a scan stop at a checkpointed boundary
    instead of being torn out mid-request. This must never touch checkpoint
    save/load semantics — only *when* the scan loop stops.
    """

    def _make_scanner(self, staging, tmp_path, shutdown_event=None, **cfg_kwargs):
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            **cfg_kwargs,
            queries=[LinkedInQueryConfig(search_term="engineer", location="France", results_wanted=1)],
        )
        return LinkedInScanner(
            config=cfg, staging=staging, config_dir=str(tmp_path), shutdown_event=shutdown_event
        )

    def test_shutdown_before_any_page_stops_without_fetching(self, staging, tmp_path) -> None:
        event = threading.Event()
        event.set()
        scanner = self._make_scanner(staging, tmp_path, shutdown_event=event, max_pages_per_query=10)
        query = scanner.config.queries[0]

        mock_scrape = MagicMock()
        q_stats = scanner._run_query("scan-shutdown-1", query, mock_scrape, None, {})

        mock_scrape.assert_not_called()
        assert q_stats.stopped_by == "shutdown"
        assert q_stats.pages_fetched == 0

    def test_shutdown_between_pages_preserves_checkpoint_at_last_fetched_page(self, staging, tmp_path) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, tmp_path, shutdown_event=event, max_pages_per_query=10, max_results_per_query=1)
        query = scanner.config.queries[0]

        pages = [_make_mock_df([_make_df_row("sd0001")]), _make_mock_df([_make_df_row("sd0002")])]

        def scrape_then_request_shutdown(**kwargs):
            page = kwargs["offset"]
            if page >= 2:
                raise AssertionError("must not fetch a page after shutdown was requested")
            result = pages[page]
            if page == 1:
                # Simulate the backend shutdown handler firing while this
                # page's request was in flight -- by the time control
                # returns to the loop, a stop has been requested.
                event.set()
            return result

        q_stats = scanner._run_query("scan-shutdown-2", query, scrape_then_request_shutdown, None, {})

        assert q_stats.stopped_by == "shutdown"
        assert q_stats.pages_fetched == 2  # pages 0 and 1 both fully processed

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["current_page"] == 2  # resume point is exactly the next unfetched page

        count = staging._get_conn().execute("SELECT COUNT(*) FROM linkedin_staging").fetchone()[0]
        assert count == 2  # both pre-shutdown pages' jobs were staged, nothing lost

    def test_shutdown_mid_scan_does_not_mark_scan_completed_or_clear_checkpoint(self, staging, tmp_path) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, tmp_path, shutdown_event=event, max_pages_per_query=10, max_results_per_query=1)

        def scrape_then_shutdown(**kwargs):
            offset = kwargs["offset"]
            if offset == 0:
                event.set()
                return _make_mock_df([_make_df_row("full0001")])
            raise AssertionError("must not fetch further pages once shutdown was requested")

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = scrape_then_shutdown
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status, error FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "interrupted"  # resumable, not "completed" and not "failed" (#86)
        assert "shutdown" in row["error"].lower()

        # Checkpoint must survive so the next scan resumes from page 1, not
        # from scratch and not lost entirely.
        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["current_page"] == 1

    def test_scan_resumes_correctly_after_a_shutdown_stop(self, staging, tmp_path) -> None:
        """End-to-end: a scan stopped for shutdown, then a fresh scan (new
        process/instance, no shutdown_event set) must resume exactly where
        the previous one left off -- the same resumability already proven
        for crashes in TestScanCheckpointResume, now also proven for a
        cooperative shutdown stop."""
        event = threading.Event()
        scanner = self._make_scanner(staging, tmp_path, shutdown_event=event, max_pages_per_query=10, max_results_per_query=1)

        def scrape_then_shutdown(**kwargs):
            offset = kwargs["offset"]
            if offset == 0:
                event.set()
                return _make_mock_df([_make_df_row("res0001")])
            raise AssertionError("unexpected fetch during first (shutting-down) attempt")

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = scrape_then_shutdown
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            scanner.run_scan()

        # --- Fresh scanner instance, no shutdown requested (the "next process") ---
        fresh_scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=10, max_results_per_query=1)
        offsets_requested: list[int] = []

        def resumed_scrape(**kwargs):
            offset = kwargs["offset"]
            offsets_requested.append(offset)
            if offset == 1:
                return _make_mock_df([_make_df_row("res0002")])
            return _make_mock_df([])

        mock_jobspy_2 = MagicMock()
        mock_jobspy_2.scrape_jobs = resumed_scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy_2}):
            scan_id = fresh_scanner.run_scan()

        assert 0 not in offsets_requested  # page already fetched before shutdown, not re-requested
        assert offsets_requested[0] == 1

        rows = staging._get_conn().execute(
            "SELECT linkedin_job_id, COUNT(*) as n FROM linkedin_staging GROUP BY linkedin_job_id"
        ).fetchall()
        counts = {r["linkedin_job_id"]: r["n"] for r in rows}
        assert counts == {"res0001": 1, "res0002": 1}

        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert staging.get_scan_checkpoint() is None


# ---------------------------------------------------------------------------
# A query that hits a caught transient error must remain resumable
#
# Audit finding: the per-query except handler used to unconditionally mark
# the query "completed" in the checkpoint even when it had just failed
# (network error, jobspy/scraper exception, an uncaught DB error). Since the
# whole scan still finishes normally in the common case (a per-query error
# doesn't abort _do_scan's loop), the checkpoint gets cleared at the end
# regardless and the bug is invisible — it only bites when that same scan
# attempt is ALSO interrupted for real (crash) before reaching the end,
# leaving the wrongly-"completed" query stuck unresumed until some future
# fully clean pass happens to retry everything. These tests force exactly
# that interleaving: one query fails with a caught error, then something
# else in the same scan genuinely aborts it before it finishes, and verify
# the failed query's checkpoint entry — and its jobs — are not lost.
# ---------------------------------------------------------------------------

class TestQueryErrorDoesNotBlockResume:
    def _make_scanner(self, staging, tmp_path, queries, **cfg_kwargs):
        from src.linkedin.scanner import LinkedInConfig, LinkedInScanner
        defaults = {
            "enabled": True,
            "rate_limit_seconds": 0.0,
            "max_pages_per_query": 5,
            "max_results_per_query": 5,
        }
        defaults.update(cfg_kwargs)
        cfg = LinkedInConfig(queries=queries, **defaults)
        return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

    def test_failed_query_is_not_marked_complete_in_checkpoint(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import LinkedInQueryConfig, _query_key

        q_alpha = LinkedInQueryConfig(search_term="alpha", results_wanted=5)
        q_beta = LinkedInQueryConfig(search_term="beta", results_wanted=5)
        scanner = self._make_scanner(staging, tmp_path, [q_alpha, q_beta])

        def scrape(**kwargs):
            if kwargs["search_term"] == "alpha":
                raise RuntimeError("simulated network/scraper failure")
            return _make_mock_df([])  # beta: exhausted immediately

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = scrape

        # Force the *whole scan* to fail after the per-query loop (simulating
        # a genuine interruption partway through, e.g. a DB hiccup during
        # the post-loop stats backfill) so the checkpoint is left in place
        # instead of being cleared by a clean finish — this is the only
        # window where the bug is observable.
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch.object(staging, "count_unique_to_query", side_effect=RuntimeError("db hiccup")):
            scan_id = scanner.run_scan()

        row = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "failed"

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        completed = json.loads(cp["completed_queries_json"])
        # beta genuinely finished — correctly checked off.
        assert _query_key(q_beta) in completed
        # alpha only failed — must NOT be checked off, or a resume would
        # skip it forever and its jobs would never be (re)discovered.
        assert _query_key(q_alpha) not in completed

    def test_resumed_scan_retries_the_failed_query_and_recovers_its_jobs(
        self, staging, tmp_path
    ) -> None:
        from src.linkedin.scanner import LinkedInQueryConfig

        q_alpha = LinkedInQueryConfig(search_term="alpha", results_wanted=5)
        q_beta = LinkedInQueryConfig(search_term="beta", results_wanted=5)
        scanner = self._make_scanner(staging, tmp_path, [q_alpha, q_beta])

        # --- Attempt 1: alpha fails, beta succeeds, then the scan as a
        # whole is interrupted before it can clear the checkpoint. ---
        def scrape_attempt_1(**kwargs):
            if kwargs["search_term"] == "alpha":
                raise RuntimeError("simulated network/scraper failure")
            return _make_mock_df([_make_df_row("beta0001")])

        # beta's second page (offset 1) returns empty -> exhausted.
        calls = {"beta": 0}

        def scrape_attempt_1_paged(**kwargs):
            if kwargs["search_term"] == "alpha":
                raise RuntimeError("simulated network/scraper failure")
            calls["beta"] += 1
            if calls["beta"] == 1:
                return _make_mock_df([_make_df_row("beta0001")])
            return _make_mock_df([])

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = scrape_attempt_1_paged
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch.object(staging, "count_unique_to_query", side_effect=RuntimeError("db hiccup")):
            scanner.run_scan()

        # alpha never got a chance to stage anything yet.
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging WHERE linkedin_job_id LIKE 'alpha%'"
        ).fetchone()[0]
        assert count == 0

        # --- Attempt 2: a brand new scan (retry/next scheduler tick).
        # alpha now succeeds; beta must not be re-fetched at all. ---
        beta_calls_in_attempt_2 = 0

        def scrape_attempt_2(**kwargs):
            nonlocal beta_calls_in_attempt_2
            if kwargs["search_term"] == "beta":
                beta_calls_in_attempt_2 += 1
                return _make_mock_df([])
            return _make_mock_df([_make_df_row("alpha0001")])

        mock_jobspy2 = MagicMock()
        mock_jobspy2.scrape_jobs = scrape_attempt_2
        with patch.dict("sys.modules", {"jobspy": mock_jobspy2}):
            scan_id_2 = scanner.run_scan()

        # beta was already checkpointed complete — must not be re-queried.
        assert beta_calls_in_attempt_2 == 0

        # alpha's job is now recovered — nothing was permanently missed.
        rows = staging._get_conn().execute(
            "SELECT linkedin_job_id FROM linkedin_staging"
        ).fetchall()
        job_ids = {r["linkedin_job_id"] for r in rows}
        assert job_ids == {"alpha0001", "beta0001"}

        row2 = staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id_2,)
        ).fetchone()
        assert row2["status"] == "completed"
        assert staging.get_scan_checkpoint() is None

    def test_query_that_fails_after_some_pages_resumes_from_the_last_good_page(
        self, staging, tmp_path
    ) -> None:
        """A query that succeeds on page 0 then fails on page 1 must resume
        at page 1 (not page 0, and not be skipped entirely)."""
        from src.linkedin.scanner import LinkedInQueryConfig, _query_key

        q_alpha = LinkedInQueryConfig(search_term="alpha", results_wanted=1)
        scanner = self._make_scanner(
            staging, tmp_path, [q_alpha], max_results_per_query=1, max_pages_per_query=10
        )

        def scrape_crash_on_page_1(**kwargs):
            if kwargs["offset"] == 0:
                return _make_mock_df([_make_df_row("partial0001")])
            raise RuntimeError("simulated failure on page 1")

        # With a single query that itself fails, the post-loop stats
        # backfill never touches count_unique_to_query (it's skipped for
        # stopped_by="error" queries) — force the interruption at the final
        # completion step instead, which always runs regardless of any
        # individual query's outcome.
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = scrape_crash_on_page_1
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch.object(staging, "complete_scan_run", side_effect=RuntimeError("db hiccup")):
            scanner.run_scan()

        cp = staging.get_scan_checkpoint()
        assert cp is not None
        assert cp["current_query_key"] == _query_key(q_alpha)
        assert cp["current_page"] == 1  # page 0 succeeded, resume at page 1
        assert _query_key(q_alpha) not in json.loads(cp["completed_queries_json"])

        offsets_requested: list[int] = []

        def scrape_resume(**kwargs):
            offset = kwargs["offset"]
            offsets_requested.append(offset)
            return _make_mock_df([])  # exhausted immediately

        mock_jobspy2 = MagicMock()
        mock_jobspy2.scrape_jobs = scrape_resume
        with patch.dict("sys.modules", {"jobspy": mock_jobspy2}):
            scanner.run_scan()

        assert offsets_requested == [1]  # page 0 was never re-fetched
        count = staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == 1  # partial0001 only — no duplicate, nothing lost


# ---------------------------------------------------------------------------
# Manual pause / resume (issue #81)
# ---------------------------------------------------------------------------

class TestManualPause:
    """A user pause stops at the same checkpointed boundaries as a shutdown,
    persists 'paused' (surviving restarts), and a later scan resumes from the
    exact checkpoint instead of page 0."""

    def _make_scanner(self, staging, tmp_path, shutdown_event=None, queries=None, **cfg_kwargs):
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

        cfg_kwargs.setdefault("max_pages_per_query", 10)
        cfg_kwargs.setdefault("max_results_per_query", 1)
        cfg = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            queries=queries or [
                LinkedInQueryConfig(search_term="engineer", location="France", results_wanted=1),
            ],
            **cfg_kwargs,
        )
        return LinkedInScanner(
            config=cfg, staging=staging, config_dir=str(tmp_path), shutdown_event=shutdown_event
        )

    @staticmethod
    def _run(scanner, scrape) -> str:
        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = scrape
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            return scanner.run_scan()

    @staticmethod
    def _run_status(staging, scan_id: str) -> str:
        return staging._get_conn().execute(
            "SELECT status FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()["status"]

    def _pause_after_page(self, scanner, pause_at_offset: int, requested: list[int]):
        def scrape(**kwargs):
            offset = kwargs["offset"]
            requested.append(offset)
            if offset > pause_at_offset:
                raise AssertionError("no page may be fetched after the pause was honoured")
            if offset == pause_at_offset:
                assert scanner.request_pause() is True
            return _make_mock_df([_make_df_row(f"p{offset:05d}")])
        return scrape

    def test_pause_stops_at_page_boundary_and_persists_paused(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)
        requested: list[int] = []
        scan_id = self._run(scanner, self._pause_after_page(scanner, 1, requested))

        assert requested == [0, 1]
        assert self._run_status(staging, scan_id) == "paused"
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "paused"
        assert cp["current_page"] == 2  # page 1 was fully staged before stopping
        assert scanner.last_scan_paused() is True
        assert scanner.last_scan_completed() is False
        assert scanner.scan_state() == "paused"
        assert scanner.is_paused() is True
        assert scanner.progress() == {
            "queries_completed": 0,
            "queries_total": 1,
            "current_query": "engineer",
            "current_page": 2,
            "updated_at": cp["updated_at"],
            # Work-based estimate (issue #86): no history → 10-page cap;
            # 2 of an estimated 10 pages done. No ETA while paused.
            "percent": 20,
            "pages_done_est": 2,
            "pages_total_est": 10,
            "current_query_pages_est": 10,
            "estimated": True,
            "eta_seconds": None,
            "eta_basis": None,
        }

    def test_resume_continues_from_checkpoint_not_page_zero(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)
        self._run(scanner, self._pause_after_page(scanner, 1, []))

        resumed: list[int] = []

        def scrape(**kwargs):
            resumed.append(kwargs["offset"])
            if kwargs["offset"] >= 4:
                return _make_mock_df([])
            return _make_mock_df([_make_df_row(f"r{kwargs['offset']:05d}")])

        scan_id = self._run(scanner, scrape)
        assert resumed == [2, 3, 4]  # never re-fetches pages 0 and 1
        assert self._run_status(staging, scan_id) == "completed"
        assert staging.get_scan_checkpoint() is None
        assert scanner.scan_state() == "idle"
        count = staging._get_conn().execute("SELECT COUNT(*) FROM linkedin_staging").fetchone()[0]
        assert count == 4  # p0, p1 from before the pause + r2, r3 after

    def test_pause_survives_restart_and_a_new_instance_resumes_it(self, staging, tmp_path) -> None:
        from src.linkedin.staging import LinkedInStagingStorage

        scanner = self._make_scanner(staging, tmp_path)
        self._run(scanner, self._pause_after_page(scanner, 0, []))

        # "Restart": new storage + scanner on the same DB, startup recovery runs.
        staging2 = LinkedInStagingStorage(db_path=str(staging._db_path))
        staging2.recover_stale_scans()
        scanner2 = self._make_scanner(staging2, tmp_path)
        assert scanner2.scan_state() == "paused"
        assert scanner2.has_interrupted_checkpoint() is False

        resumed: list[int] = []

        def scrape(**kwargs):
            resumed.append(kwargs["offset"])
            return _make_mock_df([])

        self._run(scanner2, scrape)
        assert resumed == [1]
        staging2.close()

    def test_pause_when_idle_is_a_noop(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)
        assert scanner.request_pause() is False
        assert scanner.scan_state() == "idle"
        assert staging.get_scan_checkpoint() is None

    def test_pause_twice_is_idempotent(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)

        def scrape(**kwargs):
            assert scanner.request_pause() is True
            assert scanner.request_pause() is True
            assert scanner.scan_state() == "pausing"
            return _make_mock_df([_make_df_row("twice001")])

        scan_id = self._run(scanner, scrape)
        assert self._run_status(staging, scan_id) == "paused"
        assert staging.get_scan_checkpoint()["current_page"] == 1

    def test_pause_right_after_completion_is_a_noop(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)
        scan_id = self._run(scanner, lambda **kw: _make_mock_df([]))
        assert scanner.request_pause() is False
        assert self._run_status(staging, scan_id) == "completed"
        assert scanner.scan_state() == "idle"

    def test_pause_requested_during_the_last_page_completes_normally(self, staging, tmp_path) -> None:
        """Completion immediately before the pause: the page the pause lands on
        is the query's last one (safety limit) and it is the last query, so
        there is no boundary left to stop at — the scan completes."""
        scanner = self._make_scanner(staging, tmp_path, max_pages_per_query=1)

        def scrape(**kwargs):
            scanner.request_pause()
            return _make_mock_df([_make_df_row("last0001")])

        scan_id = self._run(scanner, scrape)
        assert self._run_status(staging, scan_id) == "completed"
        assert staging.get_scan_checkpoint() is None
        assert scanner.last_scan_paused() is False

    def test_pause_between_queries_keeps_the_completed_query(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import LinkedInQueryConfig

        scanner = self._make_scanner(
            staging, tmp_path,
            queries=[
                LinkedInQueryConfig(search_term="a", location="France", results_wanted=1),
                LinkedInQueryConfig(search_term="b", location="France", results_wanted=1),
            ],
            max_pages_per_query=1,
        )
        terms: list[str] = []

        def scrape(**kwargs):
            terms.append(kwargs["search_term"])
            # Query "a" finishes (safety limit) — the pause lands before "b".
            scanner.request_pause()
            return _make_mock_df([_make_df_row("a0000001")])

        scan_id = self._run(scanner, scrape)
        assert terms == ["a"]
        assert self._run_status(staging, scan_id) == "paused"
        cp = staging.get_scan_checkpoint()
        assert json.loads(cp["completed_queries_json"]) == ["a\x1fFrance"]
        assert cp["state"] == "paused"
        assert scanner.progress()["queries_completed"] == 1

        resumed_terms: list[str] = []

        def scrape_resume(**kwargs):
            resumed_terms.append(kwargs["search_term"])
            return _make_mock_df([])

        self._run(scanner, scrape_resume)
        assert resumed_terms == ["b"]  # "a" is not replayed

    def test_pause_and_shutdown_together_records_paused(self, staging, tmp_path) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, tmp_path, shutdown_event=event)

        def scrape(**kwargs):
            scanner.request_pause()
            event.set()
            return _make_mock_df([_make_df_row("both0001")])

        scan_id = self._run(scanner, scrape)
        assert self._run_status(staging, scan_id) == "paused"
        assert staging.get_scan_checkpoint()["state"] == "paused"

    def test_shutdown_alone_marks_checkpoint_interrupted(self, staging, tmp_path) -> None:
        event = threading.Event()
        scanner = self._make_scanner(staging, tmp_path, shutdown_event=event)

        def scrape(**kwargs):
            event.set()
            return _make_mock_df([_make_df_row("shut0001")])

        scan_id = self._run(scanner, scrape)
        assert self._run_status(staging, scan_id) == "interrupted"  # resumable, not failed (#86)
        assert staging.get_scan_checkpoint()["state"] == "interrupted"
        assert scanner.last_scan_paused() is False

    def test_pause_during_description_delay_refetches_that_page(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(
            staging, tmp_path, enrich_descriptions=True, description_fetch_delay=30.0,
        )
        started = time.monotonic()

        def scrape(**kwargs):
            if kwargs["offset"] == 0:
                return _make_mock_df([_make_df_row("d0000000")])
            scanner.request_pause()
            return _make_mock_df([_make_df_row("d0000001", description=None)])

        with patch(
            "src.linkedin.scanner.fetch_linkedin_description_classified",
            side_effect=AssertionError("must not fetch a description after the pause"),
        ):
            scan_id = self._run(scanner, scrape)

        assert time.monotonic() - started < 10  # did not wait out the 30 s delay
        assert self._run_status(staging, scan_id) == "paused"
        # Page 1 was abandoned mid-way: the checkpoint still points at it.
        assert staging.get_scan_checkpoint()["current_page"] == 1

    def test_incompatible_checkpoint_is_reset_with_a_warning(self, staging, tmp_path, caplog) -> None:
        from src.linkedin.scanner import LinkedInQueryConfig

        scanner = self._make_scanner(staging, tmp_path)
        self._run(scanner, self._pause_after_page(scanner, 1, []))

        # Page size changes → saved page numbers would map to other offsets.
        changed = self._make_scanner(
            staging, tmp_path, max_results_per_query=5,
            queries=[LinkedInQueryConfig(search_term="engineer", location="France", results_wanted=5)],
        )
        requested: list[int] = []

        def scrape(**kwargs):
            requested.append(kwargs["offset"])
            return _make_mock_df([])

        with caplog.at_level("WARNING", logger="src.linkedin.scanner"):
            self._run(changed, scrape)
        assert requested == [0]  # restarted from the first page
        assert any("different configuration" in r.getMessage() for r in caplog.records)

    def test_legacy_checkpoint_without_fingerprint_is_resumed(self, staging, tmp_path) -> None:
        staging.save_scan_checkpoint("legacy", [], "engineer\x1fFrance", 3)
        conn = staging._get_conn()
        conn.execute("UPDATE linkedin_scan_checkpoint SET state = NULL, config_fingerprint = NULL")
        conn.commit()
        scanner = self._make_scanner(staging, tmp_path)
        assert scanner.scan_state() == "interrupted"

        requested: list[int] = []

        def scrape(**kwargs):
            requested.append(kwargs["offset"])
            return _make_mock_df([])

        self._run(scanner, scrape)
        assert requested == [3]

    def test_new_scan_clears_a_stale_pause_request(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)
        self._run(scanner, self._pause_after_page(scanner, 0, []))
        # The pause flag of the finished scan must not stop the resume.
        scan_id = self._run(scanner, lambda **kw: _make_mock_df([]))
        assert self._run_status(staging, scan_id) == "completed"


# ---------------------------------------------------------------------------
# Pause/resume review follow-ups (issue #81): persistence failures, the
# 'failed' label, and stops landing inside the interruptible waits.
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

    def test_failed_pause_persistence_is_not_reported_as_a_pause(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)

        def scrape(**kwargs):
            scanner.request_pause()
            return _make_mock_df([_make_df_row("fp000001")])

        with self._failing_state_write(staging, "paused"):
            scan_id = self._run(scanner, scrape)

        assert scanner.last_scan_paused() is False
        assert scanner.last_scan_completed() is False
        row = staging._get_conn().execute(
            "SELECT status, error FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "pause requested but could not be persisted" in row["error"].lower()
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "failed"   # consistent with the run; normal retry policy
        assert cp["current_page"] == 1  # progress kept
        assert scanner.is_paused() is False
        assert scanner.interrupted_checkpoint_id() is None  # never auto-resumed

        from src.scheduler.service import _scanner_dispatch

        with patch.object(scanner, "run_scan", return_value=scan_id):
            result = _scanner_dispatch(scanner)
        assert result.paused is False and result.completed is False

    def test_failed_pause_and_failed_label_leave_a_non_resumable_running_row(
        self, staging, tmp_path
    ) -> None:
        scanner = self._make_scanner(staging, tmp_path)

        def scrape(**kwargs):
            scanner.request_pause()
            return _make_mock_df([_make_df_row("fp000002")])

        with self._failing_state_write(staging, "paused", "failed"):
            self._run(scanner, scrape)

        assert scanner.last_scan_paused() is False
        assert staging.get_scan_checkpoint()["state"] == "running"
        # An orphaned 'running' row is not proof of an interruption while the
        # process is up; startup recovery turns it into 'interrupted'.
        assert scanner.interrupted_checkpoint_id() is None
        staging.recover_stale_scans()
        assert scanner.interrupted_checkpoint_id() is not None

    def test_scan_error_labels_the_checkpoint_failed(self, staging, tmp_path) -> None:
        import sqlite3

        scanner = self._make_scanner(staging, tmp_path)

        def scrape(**kwargs):
            if kwargs["offset"] >= 2:
                return _make_mock_df([])
            return _make_mock_df([_make_df_row(f"er{kwargs['offset']:06d}")])

        with patch.object(
            staging, "complete_scan_run", side_effect=sqlite3.OperationalError("database is locked")
        ):
            scan_id = self._run(scanner, scrape)

        assert self._run_status(staging, scan_id) == "failed"
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "failed"
        assert scanner.scan_state() == "idle"
        assert scanner.interrupted_checkpoint_id() is None

    def test_shutdown_during_description_delay_refetches_the_page(self, staging, tmp_path) -> None:
        event = threading.Event()
        scanner = self._make_scanner(
            staging, tmp_path, shutdown_event=event,
            enrich_descriptions=True, description_fetch_delay=30.0,
        )
        started = time.monotonic()

        def scrape(**kwargs):
            if kwargs["offset"] == 0:
                return _make_mock_df([_make_df_row("sd000000")])
            if kwargs["offset"] == 1:
                threading.Timer(0.3, event.set).start()  # fires inside the delay
                return _make_mock_df([_make_df_row("sd000001", description=None)])
            raise AssertionError("no page after the shutdown")

        with patch(
            "src.linkedin.scanner.fetch_linkedin_description_classified",
            side_effect=AssertionError("must not fetch a description after the shutdown"),
        ):
            scan_id = self._run(scanner, scrape)

        assert time.monotonic() - started < 10
        row = staging._get_conn().execute(
            "SELECT status, error FROM linkedin_scan_runs WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "interrupted" and "shutdown" in row["error"].lower()
        cp = staging.get_scan_checkpoint()
        assert cp["state"] == "interrupted"
        assert cp["current_page"] == 1  # the abandoned page is re-fetched on resume

        resumed: list[int] = []

        def scrape_resume(**kwargs):
            resumed.append(kwargs["offset"])
            return _make_mock_df([])

        fresh = self._make_scanner(staging, tmp_path)
        self._run(fresh, scrape_resume)
        assert resumed == [1]

    def test_pause_interrupts_a_nonzero_page_rate_limit_wait(self, staging, tmp_path) -> None:
        scanner = self._make_scanner(staging, tmp_path)
        scanner.config.rate_limit_seconds = 30.0
        requested: list[int] = []
        started = time.monotonic()

        def scrape(**kwargs):
            requested.append(kwargs["offset"])
            if kwargs["offset"] == 0:
                threading.Timer(0.3, scanner.request_pause).start()  # lands in the 30 s wait
                return _make_mock_df([_make_df_row("rl000000")])
            raise AssertionError("no page after the pause")

        scan_id = self._run(scanner, scrape)
        assert time.monotonic() - started < 10  # did not sit out the 30 s wait
        assert requested == [0]
        assert self._run_status(staging, scan_id) == "paused"
        assert staging.get_scan_checkpoint()["current_page"] == 1

    def test_shutdown_interrupts_a_nonzero_query_rate_limit_wait(self, staging, tmp_path) -> None:
        from src.linkedin.scanner import LinkedInQueryConfig

        event = threading.Event()
        scanner = self._make_scanner(
            staging, tmp_path, shutdown_event=event, max_pages_per_query=1,
            queries=[
                LinkedInQueryConfig(search_term="a", location="France", results_wanted=1),
                LinkedInQueryConfig(search_term="b", location="France", results_wanted=1),
            ],
        )
        scanner.config.rate_limit_seconds = 30.0
        terms: list[str] = []
        started = time.monotonic()

        def scrape(**kwargs):
            terms.append(kwargs["search_term"])
            threading.Timer(0.3, event.set).start()  # lands in the between-query wait
            return _make_mock_df([_make_df_row("qw000001")])

        scan_id = self._run(scanner, scrape)
        assert time.monotonic() - started < 10
        assert terms == ["a"]
        assert self._run_status(staging, scan_id) == "interrupted"  # resumable, not failed (#86)
        cp = staging.get_scan_checkpoint()
        assert json.loads(cp["completed_queries_json"]) == ["a\x1fFrance"]
        assert cp["state"] == "interrupted"


class TestDurablePauseSurvivesLoggingErrors:
    """Issue #81 re-review (R1): once the pause is durable, a failure while
    reading progress for the log line must not turn it into a failed scan."""

    _make_scanner = TestManualPause._make_scanner
    _run = staticmethod(TestManualPause._run)
    _run_status = staticmethod(TestManualPause._run_status)

    def test_progress_read_error_after_a_persisted_pause_keeps_it_paused(
        self, staging, tmp_path
    ) -> None:
        import sqlite3

        scanner = self._make_scanner(staging, tmp_path)
        real_set = staging.set_checkpoint_state
        real_get = staging.get_scan_checkpoint
        paused_written = []

        def spy_set(scan_id, state, create_if_missing=True):
            real_set(scan_id, state, create_if_missing)
            if state == "paused":
                paused_written.append(scan_id)

        def flaky_get():
            if paused_written:
                raise sqlite3.OperationalError("database is locked")
            return real_get()

        def scrape(**kwargs):
            scanner.request_pause()
            return _make_mock_df([_make_df_row("dp000001")])

        with patch.object(staging, "set_checkpoint_state", side_effect=spy_set), \
             patch.object(staging, "get_scan_checkpoint", side_effect=flaky_get):
            scan_id = self._run(scanner, scrape)

        assert paused_written == [scan_id]  # the pause itself was persisted
        assert real_get()["state"] == "paused"
        assert self._run_status(staging, scan_id) == "paused"
        assert scanner.last_scan_paused() is True
        assert scanner.last_scan_completed() is False
        assert scanner.is_paused() is True
