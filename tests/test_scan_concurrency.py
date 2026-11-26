"""Cross-cutting concurrency regression tests.

These exercise scenarios that don't fit neatly inside a single module's test
file: normal API/dashboard-style database access happening while a scanner
is actively writing to the same jobs.db file. All tests use isolated
temporary databases — the real data/jobs.db is never touched.

Background: users reported "database is locked" during scans, and scans
appearing to reset to the beginning while using the app at the same time.
Root cause (see src/storage/sqlite.py, src/hellowork/staging.py,
src/hellowork/scanner.py; the removed LinkedIn scanner shared the same pattern):
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

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


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
        a brand new Storage().init_db() call) while the HelloWork scanner is
        concurrently inserting jobs — neither side should raise, and no
        staged job should be lost.
        """
        from src.hellowork.staging import HelloWorkStagingStorage
        from src.storage.runs import RunStorage
        from src.storage.sqlite import Storage

        hw_staging = HelloWorkStagingStorage(db_path=tmp_db)
        hw_staging.init_db()

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
            hw_staging.create_scan_run("scan-under-load")
            for i in range(n_jobs):
                job_id = f"job-{i}"
                try:
                    hw_staging.insert_job({
                        "id": hashlib.sha256(f"hellowork:{job_id}".encode()).hexdigest()[:32],
                        "source_job_id": job_id,
                        "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{i}.html",
                        "title": f"Job {i}", "company": "Acme", "location": "Paris",
                        "countries": json_module.dumps(["FR"]), "is_remote": 0,
                        "work_mode": None, "employment_type": None,
                        "salary_min": None, "salary_max": None, "salary_currency": "EUR",
                        "description": "desc", "published_at": None,
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

        count = hw_staging._get_conn().execute(
            "SELECT COUNT(*) FROM hellowork_staging"
        ).fetchone()[0]
        assert count == n_jobs

        hw_staging.close()


# ---------------------------------------------------------------------------
# Resumable scan checkpoint: concurrency safety
# ---------------------------------------------------------------------------

class TestScanCheckpointConcurrency:
    def test_concurrent_checkpoint_writes_and_api_reads_do_not_corrupt_progress(
        self, tmp_db: str
    ) -> None:
        """A scanner thread persisting its checkpoint after every page, while
        an API-polling thread repeatedly reads scan status (fresh
        Storage-style instances, matching real request handling) — the
        checkpoint must always be readable as one of the values the writer
        actually wrote (a growing list of completed slugs), never a corrupted
        or torn read, and no exception on either side.
        """
        from src.hellowork.staging import HelloWorkStagingStorage

        staging = HelloWorkStagingStorage(db_path=tmp_db)
        staging.init_db()

        n_pages = 100
        slugs = [f"slug-{i}" for i in range(n_pages)]
        writer_errors: list[Exception] = []
        reader_errors: list[Exception] = []
        observed_pages: list[int] = []
        stop = threading.Event()

        def writer() -> None:
            for page in range(n_pages):
                try:
                    staging.save_scan_checkpoint("scan-1", slugs[:page + 1])
                except Exception as exc:  # noqa: BLE001
                    writer_errors.append(exc)

        def reader() -> None:
            reader_staging = HelloWorkStagingStorage(db_path=tmp_db)
            while not stop.is_set():
                try:
                    cp = reader_staging.get_scan_checkpoint()
                    if cp is not None:
                        observed_pages.append(len(json.loads(cp["completed_slugs_json"])) - 1)
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
        assert json.loads(final_cp["completed_slugs_json"]) == slugs

        staging.close()
