"""Cross-cutting concurrency regression tests.

These exercise scenarios that don't fit neatly inside a single module's test
file: LinkedIn and HelloWork scanning independently against the same jobs.db
file at the same time, and normal API/dashboard-style database access
happening while a scanner is actively writing. All tests use isolated
temporary databases — the real data/jobs.db is never touched.

Background: users reported "database is locked" during scans, and scans
appearing to reset to the beginning while using the app at the same time.
Root cause (see src/storage/sqlite.py, src/linkedin/staging.py,
src/hellowork/staging.py, src/linkedin/scanner.py, src/hellowork/scanner.py):
a fresh Storage/RunStorage/*StagingStorage instance is constructed on every
API request or pipeline run, and its init_db() used to re-run full-table
migration writes every single time; a large pipeline batch upsert held
SQLite's single writer lock for the whole run; and an uncaught lock error on
the very first scan write silently killed the scan thread. All three are
fixed; these tests lock the fix in against regression.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Independent LinkedIn / HelloWork scan state
# ---------------------------------------------------------------------------

class TestIndependentScannerState:
    def test_linkedin_and_hellowork_scan_concurrently_without_interference(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        # Pre-import src.pipeline (pulls in sentence-transformers) on the main
        # thread first, exactly like the FastAPI lifespan does in production
        # before any scanner thread starts (see
        # src/linkedin/scanner.py::_load_filters_config's docstring). Without
        # this, two scanner threads racing to trigger this same heavy,
        # non-thread-safe cold import concurrently is a test artifact, not a
        # production scenario, and can crash the import machinery itself.
        import src.pipeline.normalize  # noqa: F401
        from src.collectors.base import CollectorResult
        from src.hellowork.scanner import HelloWorkScanner
        from src.hellowork.staging import HelloWorkStagingStorage
        from src.linkedin.scanner import (
            LinkedInConfig,
            LinkedInQueryConfig,
            LinkedInScanner,
        )
        from src.linkedin.staging import LinkedInStagingStorage

        li_staging = LinkedInStagingStorage(db_path=tmp_db)
        li_staging.init_db()
        hw_staging = HelloWorkStagingStorage(db_path=tmp_db)
        hw_staging.init_db()

        li_config = LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            fetch_description=False,
            queries=[LinkedInQueryConfig(search_term="developer", location="France", results_wanted=5)],
        )
        li_scanner = LinkedInScanner(config=li_config, staging=li_staging, config_dir=str(tmp_path))
        hw_scanner = HelloWorkScanner(config={"metier_slugs": ["developpeur"]}, staging=hw_staging)

        li_started = threading.Event()
        li_release = threading.Event()

        def slow_scrape(**_kwargs: Any) -> MagicMock:
            li_started.set()
            li_release.wait(timeout=5)
            df = MagicMock()
            df.empty = True
            return df

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = slow_scrape

        hw_started = threading.Event()
        hw_release = threading.Event()

        def slow_collect(*_args: Any, **_kwargs: Any) -> CollectorResult:
            hw_started.set()
            hw_release.wait(timeout=5)
            return CollectorResult(source_name="hellowork", jobs=[], errors=[], duration_seconds=0.1)

        with patch.dict("sys.modules", {"jobspy": mock_jobspy}), \
             patch("src.collectors.hellowork.HelloWorkCollector.collect", side_effect=slow_collect):
            li_thread = threading.Thread(target=li_scanner.run_scan)
            hw_thread = threading.Thread(target=hw_scanner.run_scan)
            li_thread.start()
            hw_thread.start()

            assert li_started.wait(timeout=5)
            assert hw_started.wait(timeout=5)

            # Both run truly concurrently — neither's is_scanning() flag or
            # lock is affected by the other being mid-scan.
            assert li_scanner.is_scanning() is True
            assert hw_scanner.is_scanning() is True

            li_release.set()
            hw_release.set()
            li_thread.join(timeout=5)
            hw_thread.join(timeout=5)

        assert li_scanner.is_scanning() is False
        assert hw_scanner.is_scanning() is False

        li_last = li_staging.get_last_scan()
        hw_last = hw_staging.get_status()["last_scan"]
        assert li_last is not None and li_last["status"] == "completed"
        assert hw_last is not None and hw_last["status"] == "completed"

        li_staging.close()
        hw_staging.close()

    def test_hellowork_snapshot_does_not_touch_linkedin_waiting_rows(self, tmp_db: str) -> None:
        """Independent persistent progress: snapshotting one source's waiting
        queue must never affect the other source's queue, even though both
        live in the same jobs.db file."""
        import hashlib
        import json as json_module
        from datetime import UTC, datetime

        from src.hellowork.staging import HelloWorkStagingStorage
        from src.linkedin.staging import LinkedInStagingStorage

        li_staging = LinkedInStagingStorage(db_path=tmp_db)
        li_staging.init_db()
        hw_staging = HelloWorkStagingStorage(db_path=tmp_db)
        hw_staging.init_db()

        now = datetime.now(UTC).isoformat()
        li_id = "999888777"
        li_staging.create_scan_run("li-scan-1")
        li_staging.insert_job({
            "id": hashlib.sha256(f"linkedin:{li_id}".encode()).hexdigest()[:32],
            "linkedin_job_id": li_id,
            "canonical_url": f"https://www.linkedin.com/jobs/view/{li_id}",
            "title": "LI Job", "company": "Acme", "location": "Paris",
            "countries": json_module.dumps(["FR"]), "is_remote": 0,
            "work_mode": None, "employment_type": None,
            "salary_min": None, "salary_max": None, "salary_currency": "EUR",
            "description": "desc", "description_fetched": 1, "published_at": None,
            "raw_payload": "{}", "scan_run_id": "li-scan-1",
            "discovered_at": now, "last_seen_at": now,
        })

        hw_id = "111222333"
        hw_staging.create_scan_run("hw-scan-1")
        hw_staging.insert_job({
            "id": hashlib.sha256(f"hellowork:{hw_id}".encode()).hexdigest()[:32],
            "source_job_id": hw_id,
            "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{hw_id}.html",
            "title": "HW Job", "company": "Acme", "location": "Lyon",
            "countries": json_module.dumps(["FR"]), "is_remote": 0,
            "work_mode": None, "employment_type": None,
            "salary_min": None, "salary_max": None, "salary_currency": "EUR",
            "description": "desc", "published_at": None,
            "raw_payload": "{}", "scan_run_id": "hw-scan-1",
            "discovered_at": now, "last_seen_at": now,
        })

        # Snapshot HelloWork only.
        hw_snapshot = hw_staging.snapshot_waiting("pipeline-run-1")
        assert len(hw_snapshot) == 1

        # LinkedIn's row must be completely untouched — still 'waiting'.
        li_row = li_staging._get_conn().execute(
            "SELECT status FROM linkedin_staging WHERE linkedin_job_id = ?", (li_id,)
        ).fetchone()
        assert li_row["status"] == "waiting"

        li_staging.close()
        hw_staging.close()


# ---------------------------------------------------------------------------
# Concurrent scanner + API/database (dashboard-polling-style) activity
# ---------------------------------------------------------------------------

class TestConcurrentScannerAndApiActivity:
    def test_repeated_api_style_storage_construction_does_not_block_scanner(
        self, tmp_db: str
    ) -> None:
        """Regression for "scanner progress reset by normal API requests":
        every jobs.py/pipeline.py/dashboard.py route constructs a *fresh*
        Storage/RunStorage and calls init_db() on every single request. This
        simulates a dashboard hammering the API with GET requests (each one
        a brand new Storage().init_db() call) while the LinkedIn scanner is
        concurrently inserting jobs — neither side should raise, and no
        staged job should be lost.
        """
        from src.linkedin.staging import LinkedInStagingStorage
        from src.storage.runs import RunStorage
        from src.storage.sqlite import Storage

        li_staging = LinkedInStagingStorage(db_path=tmp_db)
        li_staging.init_db()

        stop = threading.Event()
        api_errors: list[Exception] = []

        def api_poll_loop() -> None:
            while not stop.is_set():
                try:
                    s = Storage(db_path=tmp_db)
                    s.init_db()
                    s.count_jobs()
                    s.close()
                    rs = RunStorage(db_path=tmp_db)
                    rs.init_db()
                    rs.close()
                except Exception as exc:  # noqa: BLE001
                    api_errors.append(exc)

        poller = threading.Thread(target=api_poll_loop, daemon=True)
        poller.start()

        import hashlib
        import json as json_module
        from datetime import UTC, datetime

        scanner_errors: list[Exception] = []
        n_jobs = 100

        def scan_insert_loop() -> None:
            now = datetime.now(UTC).isoformat()
            li_staging.create_scan_run("scan-under-load")
            for i in range(n_jobs):
                job_id = f"job-{i}"
                try:
                    li_staging.insert_job({
                        "id": hashlib.sha256(f"linkedin:{job_id}".encode()).hexdigest()[:32],
                        "linkedin_job_id": job_id,
                        "canonical_url": f"https://www.linkedin.com/jobs/view/{job_id}",
                        "title": f"Job {i}", "company": "Acme", "location": "Paris",
                        "countries": json_module.dumps(["FR"]), "is_remote": 0,
                        "work_mode": None, "employment_type": None,
                        "salary_min": None, "salary_max": None, "salary_currency": "EUR",
                        "description": "desc", "description_fetched": 1, "published_at": None,
                        "raw_payload": "{}", "scan_run_id": "scan-under-load",
                        "discovered_at": now, "last_seen_at": now,
                    })
                except Exception as exc:  # noqa: BLE001
                    scanner_errors.append(exc)

        scan_insert_loop()
        stop.set()
        poller.join(timeout=5)

        assert not scanner_errors, f"scanner insert hit: {scanner_errors!r}"
        assert not api_errors, f"API-style polling hit: {api_errors!r}"

        count = li_staging._get_conn().execute(
            "SELECT COUNT(*) FROM linkedin_staging"
        ).fetchone()[0]
        assert count == n_jobs

        li_staging.close()


# ---------------------------------------------------------------------------
# Resumable scan checkpoint: independence + concurrency safety
# ---------------------------------------------------------------------------

class TestScanCheckpointIndependenceAndConcurrency:
    def test_linkedin_and_hellowork_checkpoints_are_fully_independent(self, tmp_db: str) -> None:
        from src.hellowork.staging import HelloWorkStagingStorage
        from src.linkedin.staging import LinkedInStagingStorage

        li_staging = LinkedInStagingStorage(db_path=tmp_db)
        li_staging.init_db()
        hw_staging = HelloWorkStagingStorage(db_path=tmp_db)
        hw_staging.init_db()

        li_staging.save_scan_checkpoint("li-scan", ["term-a\x1floc"], "term-b\x1floc", 3)
        hw_staging.save_scan_checkpoint("hw-scan", ["slug-a", "slug-b"])

        # Clearing one must not touch the other.
        li_staging.clear_scan_checkpoint()
        assert li_staging.get_scan_checkpoint() is None
        hw_cp = hw_staging.get_scan_checkpoint()
        assert hw_cp is not None
        assert json.loads(hw_cp["completed_slugs_json"]) == ["slug-a", "slug-b"]

        # And vice versa.
        li_staging.save_scan_checkpoint("li-scan-2", [], "term-c\x1floc", 1)
        hw_staging.clear_scan_checkpoint()
        assert hw_staging.get_scan_checkpoint() is None
        li_cp = li_staging.get_scan_checkpoint()
        assert li_cp is not None
        assert li_cp["current_page"] == 1

        li_staging.close()
        hw_staging.close()

    def test_concurrent_checkpoint_writes_and_api_reads_do_not_corrupt_progress(
        self, tmp_db: str
    ) -> None:
        """A scanner thread persisting its checkpoint after every page, while
        an API-polling thread repeatedly reads scan status (fresh
        Storage-style instances, matching real request handling) — the
        checkpoint must always be readable as one of the values the writer
        actually wrote (monotonically increasing pages), never a corrupted
        or torn read, and no exception on either side.
        """
        from src.linkedin.staging import LinkedInStagingStorage

        staging = LinkedInStagingStorage(db_path=tmp_db)
        staging.init_db()

        n_pages = 100
        query_key = "term\x1floc"
        writer_errors: list[Exception] = []
        reader_errors: list[Exception] = []
        observed_pages: list[int] = []
        stop = threading.Event()

        def writer() -> None:
            for page in range(n_pages):
                try:
                    staging.save_scan_checkpoint("scan-1", [], query_key, page)
                except Exception as exc:  # noqa: BLE001
                    writer_errors.append(exc)

        def reader() -> None:
            reader_staging = LinkedInStagingStorage(db_path=tmp_db)
            while not stop.is_set():
                try:
                    cp = reader_staging.get_scan_checkpoint()
                    if cp is not None:
                        observed_pages.append(cp["current_page"])
                except Exception as exc:  # noqa: BLE001
                    reader_errors.append(exc)
            reader_staging.close()

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        r.start()
        w.start()
        w.join(timeout=15)
        stop.set()
        r.join(timeout=5)

        assert not writer_errors, f"checkpoint writer hit: {writer_errors!r}"
        assert not reader_errors, f"checkpoint reader hit: {reader_errors!r}"

        # Every observed page value must be one the writer actually wrote
        # (0..n_pages-1) — never garbage, never a partial/torn value, and the
        # final state must be exactly the writer's last write.
        assert all(0 <= p < n_pages for p in observed_pages)
        final_cp = staging.get_scan_checkpoint()
        assert final_cp is not None
        assert final_cp["current_page"] == n_pages - 1
        assert final_cp["current_query_key"] == query_key

        staging.close()
