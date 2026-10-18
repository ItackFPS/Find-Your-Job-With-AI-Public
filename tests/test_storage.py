"""Tests for the SQLite storage layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models.job import Job
from src.models.source import Source
from src.storage.sqlite import Storage


@pytest.fixture
def temp_db():
    """Create a unique temporary database file for testing."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield str(db_path)
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass  # Windows keeps handles open; file is safe to leave as temp artifact


class TestStorage:
    def test_init_and_count(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()
        assert storage.count_jobs() == 0

    def test_get_jobs_by_ids_batch(self, temp_db):
        """get_jobs_by_ids returns a dict keyed by id, omitting unknown ids,
        avoiding the N+1 per-id get_job_by_id loop callers previously used."""
        storage = Storage(temp_db)
        storage.init_db()

        for i in range(3):
            storage.upsert_job(Job(
                id=f"batch-{i}",
                source="greenhouse",
                title=f"Job {i}",
                company="Test Corp",
            ))

        result = storage.get_jobs_by_ids(["batch-0", "batch-2", "missing-id"])

        assert set(result.keys()) == {"batch-0", "batch-2"}
        assert result["batch-0"].title == "Job 0"
        assert result["batch-2"].title == "Job 2"

    def test_get_jobs_by_ids_empty_input(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()
        assert storage.get_jobs_by_ids([]) == {}

    def test_upsert_source(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        src = Source(name="Test", type="test")
        storage.upsert_source(src)

        sources = storage.get_active_sources()
        assert len(sources) == 1
        assert sources[0].name == "Test"

    def test_upsert_job(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(
            id="test-1",
            source="greenhouse",
            title="Test Engineer",
            company="Test Corp",
        )
        storage.upsert_job(job)

        assert storage.count_jobs() == 1
        jobs = storage.get_jobs(limit=10)
        assert len(jobs) == 1
        assert jobs[0].title == "Test Engineer"

    def test_upsert_duplicate(self, temp_db):
        """Upserting the same job twice should not create duplicates."""
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(
            id="test-1",
            source="greenhouse",
            title="Test Engineer",
            company="Test Corp",
        )
        storage.upsert_job(job)
        storage.upsert_job(job)  # Same ID

        assert storage.count_jobs() == 1

    def test_get_jobs_by_source(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job1 = Job(id="j1", source="greenhouse", title="J1")
        job2 = Job(id="j2", source="ashby", title="J2")
        job3 = Job(id="j3", source="greenhouse", title="J3")

        storage.upsert_job(job1)
        storage.upsert_job(job2)
        storage.upsert_job(job3)

        greenhouse_jobs = storage.get_jobs_by_source("greenhouse")
        assert len(greenhouse_jobs) == 2
        assert all(j.title in ("J1", "J3") for j in greenhouse_jobs)

    def test_search_jobs(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job1 = Job(id="j1", source="greenhouse", title="Software Engineer", description="Backend dev")
        job2 = Job(id="j2", source="ashby", title="Frontend Dev", description="UI work")

        storage.upsert_job(job1)
        storage.upsert_job(job2)

        results = storage.search_jobs("engineer")
        assert len(results) == 1
        assert results[0].title == "Software Engineer"

    def test_department_and_work_mode_round_trip(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(
            id="j-dept",
            source="france_travail",
            title="Dev",
            department="75",
            work_mode="hybrid",
        )
        storage.upsert_job(job)

        stored = storage.get_job_by_id("j-dept")
        assert stored is not None
        assert stored.department == "75"
        assert stored.work_mode == "hybrid"

    def test_department_and_work_mode_default_to_none(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(id="j-none", source="test", title="Dev")
        storage.upsert_job(job)

        stored = storage.get_job_by_id("j-none")
        assert stored is not None
        assert stored.department is None
        assert stored.work_mode is None

    def test_upsert_jobs_batch_round_trips_department_and_work_mode(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        jobs = [
            Job(id="b1", source="test", title="A", department="69", work_mode="remote"),
            Job(id="b2", source="test", title="B"),
        ]
        storage.upsert_jobs_batch(jobs)

        b1 = storage.get_job_by_id("b1")
        b2 = storage.get_job_by_id("b2")
        assert b1 is not None
        assert b1.department == "69"
        assert b1.work_mode == "remote"
        assert b2 is not None
        assert b2.department is None
        assert b2.work_mode is None


class TestUpsertJobsBatchChunkedCommits:
    """Regression tests for the chunked-commit fix in upsert_jobs_batch().

    Before this fix, a single pipeline run committed the entire jobs batch in
    one transaction, holding SQLite's sole writer lock for the whole loop.
    For a large run (thousands of jobs) this routinely exceeded the 5s
    busy_timeout of concurrent writers (the LinkedIn/HelloWork scanners),
    surfacing as "database is locked". Chunked commits bound how long the
    lock is held at a stretch.
    """

    def test_large_batch_commits_periodically(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()
        storage._UPSERT_CHUNK_SIZE = 10  # instance override of the class default

        conn = storage._get_conn()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            jobs = [Job(id=f"job-{i}", source="test", title=f"T{i}") for i in range(25)]
            stats = storage.upsert_jobs_batch(jobs)
        finally:
            conn.set_trace_callback(None)

        commit_count = sum(1 for s in statements if s.strip().upper() == "COMMIT")
        # 25 rows at chunk size 10 -> commits after row 10, row 20, and a
        # final commit for the trailing partial chunk (5 rows) = 3 commits.
        assert commit_count == 3
        # Phase NEXT / I11: by_source is an additive key on this return dict
        # (see Storage.upsert_jobs_batch's docstring) — all 25 jobs share the
        # same "test" source here.
        assert stats == {
            "inserted": 25,
            "updated": 0,
            "unchanged": 0,
            "by_source": {"test": {"inserted": 25, "updated": 0, "unchanged": 0}},
        }
        assert storage.count_jobs() == 25

    def test_small_batch_still_commits_once_at_the_end(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()
        # Default chunk size (200) is larger than this batch.

        conn = storage._get_conn()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            jobs = [Job(id=f"job-{i}", source="test", title=f"T{i}") for i in range(3)]
            storage.upsert_jobs_batch(jobs)
        finally:
            conn.set_trace_callback(None)

        commit_count = sum(1 for s in statements if s.strip().upper() == "COMMIT")
        assert commit_count == 1
        assert storage.count_jobs() == 3

    def test_concurrent_writer_succeeds_during_large_batch_upsert(self, temp_db, monkeypatch):
        """A concurrent writer (standing in for the scanner) must be able to
        write while a large batch upsert is in progress — proving the writer
        lock is released periodically instead of held for the whole batch.

        sqlite3.Connection methods can't be monkeypatched directly (its
        attributes are read-only), so the per-row slowdown is injected via
        json.dumps — called several times per job inside the upsert loop —
        which is enough to make the unchunked transaction span far longer
        than the concurrent writer's busy_timeout below.
        """
        import json as json_module
        import threading
        import time

        storage = Storage(temp_db)
        storage.init_db()
        storage._UPSERT_CHUNK_SIZE = 10  # small chunks -> short per-chunk lock hold

        real_dumps = json_module.dumps

        def slow_dumps(*args, **kwargs):
            time.sleep(0.01)
            return real_dumps(*args, **kwargs)

        monkeypatch.setattr(json_module, "dumps", slow_dumps)

        jobs = [Job(id=f"job-{i}", source="test", title=f"T{i}") for i in range(150)]

        errors: list[Exception] = []

        def other_writer() -> None:
            try:
                # Generous busy_timeout relative to a single chunk's expected
                # hold time (well under 1s even with the slowdown above) —
                # this proves the lock is released *periodically*, not that
                # the writer wins a narrow race. A large safety margin keeps
                # this stable under the scheduling jitter of a full test run
                # (many other tests' threads competing for the GIL).
                other = Storage(temp_db)
                other_conn = other._get_conn()
                other_conn.execute("PRAGMA busy_timeout=4000")
                other.upsert_job(Job(id="concurrent-job", source="test", title="Concurrent"))
                other.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=other_writer)
        t.start()
        try:
            storage.upsert_jobs_batch(jobs)
        finally:
            t.join(timeout=20)

        assert not errors, f"concurrent writer hit an error: {errors!r}"
        assert storage.get_job_by_id("concurrent-job") is not None
        assert storage.count_jobs() == 151


class TestContentHashGuarantee:
    """P3 — content_hash must always be a non-NULL string in the DB."""

    def test_pipeline_job_has_content_hash(self, temp_db):
        """Jobs created by the normalizer always have a non-empty content_hash."""
        from src.pipeline.normalize import normalize_job
        storage = Storage(temp_db)
        storage.init_db()

        job = normalize_job("test", {"title": "Engineer", "description": "Build things"})
        assert job.content_hash != ""
        storage.upsert_job(job)

        stored = storage.get_job_by_id(job.id)
        assert stored is not None
        assert stored.content_hash != ""

    def test_job_with_empty_content_hash_is_stored_as_empty_string(self, temp_db):
        """A Job with content_hash='' is stored and retrieved without errors."""
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(id="j-empty", source="test", title="No hash", content_hash="")
        storage.upsert_job(job)

        stored = storage.get_job_by_id("j-empty")
        assert stored is not None
        assert stored.content_hash == ""

    def test_null_content_hash_in_db_is_coerced_on_read(self, temp_db):
        """Rows with NULL content_hash (direct SQL insert) are read as '' — no 500 error."""
        import sqlite3

        # Insert directly without content_hash, bypassing the storage layer.
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        conn = sqlite3.connect(temp_db)
        conn.execute("PRAGMA journal_mode=WAL")
        # We need the table to exist first.
        storage = Storage(temp_db)
        storage.init_db()
        storage.close()

        conn.execute(
            """INSERT INTO jobs
               (id, source, title, discovered_at, countries, skills, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("j-null", "direct", "Direct Insert", now, "[]", "[]", "{}"),
        )
        conn.commit()
        conn.close()

        # Reading via the storage layer must not raise a Pydantic validation error.
        storage2 = Storage(temp_db)
        storage2.init_db()
        job = storage2.get_job_by_id("j-null")
        assert job is not None
        assert job.content_hash == ""
        storage2.close()

    def test_init_db_migration_fixes_null_rows(self, temp_db):
        """init_db() updates pre-existing NULL content_hash rows to ''."""
        import sqlite3
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        # Create table and insert a NULL-content_hash row before migration.
        conn = sqlite3.connect(temp_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                description TEXT,
                location TEXT,
                countries TEXT NOT NULL DEFAULT '[]',
                remote INTEGER NOT NULL DEFAULT 0,
                employment_type TEXT,
                salary_min REAL, salary_max REAL,
                salary_currency TEXT NOT NULL DEFAULT 'EUR',
                published_at TEXT, updated_at TEXT,
                discovered_at TEXT NOT NULL,
                skills TEXT NOT NULL DEFAULT '[]',
                raw_payload TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT,
                first_seen_at TEXT, last_seen_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source_job_id TEXT, canonical_url TEXT, apply_url TEXT, company TEXT
            )
        """)
        conn.execute(
            "INSERT INTO jobs (id, source, discovered_at, content_hash) VALUES (?, ?, ?, NULL)",
            ("j-old", "eures", now),
        )
        conn.commit()
        conn.close()

        # Running init_db() on this DB must fix the NULL row.
        # source='eures' is used because the canonical_url purge migration
        # excludes it (EURES never has canonical URLs).
        storage = Storage(temp_db)
        storage.init_db()
        job = storage.get_job_by_id("j-old")
        assert job is not None
        assert job.content_hash == ""
        storage.close()

    def test_init_db_migrates_legacy_table_missing_department_and_work_mode(self, temp_db):
        """init_db() adds department/work_mode columns to pre-existing tables
        created before these fields existed (ALTER TABLE ADD COLUMN migration)."""
        import sqlite3
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        conn = sqlite3.connect(temp_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                description TEXT,
                location TEXT,
                countries TEXT NOT NULL DEFAULT '[]',
                remote INTEGER NOT NULL DEFAULT 0,
                employment_type TEXT,
                salary_min REAL, salary_max REAL,
                salary_currency TEXT NOT NULL DEFAULT 'EUR',
                published_at TEXT, updated_at TEXT,
                discovered_at TEXT NOT NULL,
                skills TEXT NOT NULL DEFAULT '[]',
                raw_payload TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT,
                first_seen_at TEXT, last_seen_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source_job_id TEXT, canonical_url TEXT, apply_url TEXT, company TEXT
            )
        """)
        conn.execute(
            "INSERT INTO jobs (id, source, discovered_at, content_hash) VALUES (?, ?, ?, '')",
            ("j-legacy", "eures", now),
        )
        conn.commit()
        conn.close()

        # source='eures' is used because the canonical_url purge migration
        # excludes it (EURES never has canonical URLs).
        storage = Storage(temp_db)
        storage.init_db()
        job = storage.get_job_by_id("j-legacy")
        assert job is not None
        assert job.department is None
        assert job.work_mode is None

        new_job = Job(id="j-new", source="test", title="New", department="2A", work_mode="on_site")
        storage.upsert_job(new_job)
        stored = storage.get_job_by_id("j-new")
        assert stored is not None
        assert stored.department == "2A"
        assert stored.work_mode == "on_site"
        storage.close()

    def test_upsert_job_never_writes_null_content_hash(self, temp_db):
        """upsert_job() writes '' even if content_hash is somehow falsy."""
        import sqlite3
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(id="j-defense", source="test", title="Test")
        # Bypass model validation by directly setting the attribute.
        object.__setattr__(job, "content_hash", None)  # type: ignore[arg-type]
        storage.upsert_job(job)

        conn = sqlite3.connect(temp_db)
        row = conn.execute("SELECT content_hash FROM jobs WHERE id = ?", ("j-defense",)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == ""
        storage.close()


class TestCanonicalUrlRoundTrip:
    """canonical_url and apply_url must survive the full store → retrieve cycle."""

    def test_canonical_url_and_apply_url_round_trip(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(
            id="j-url",
            source="arbeitnow",
            title="Backend Engineer",
            canonical_url="https://www.arbeitnow.com/jobs/backend-engineer-123",
            apply_url="https://www.arbeitnow.com/jobs/backend-engineer-123/apply",
        )
        storage.upsert_job(job)

        stored = storage.get_job_by_id("j-url")
        assert stored is not None
        assert stored.canonical_url == "https://www.arbeitnow.com/jobs/backend-engineer-123"
        assert stored.apply_url == "https://www.arbeitnow.com/jobs/backend-engineer-123/apply"

    def test_canonical_url_none_when_not_provided(self, temp_db):
        storage = Storage(temp_db)
        storage.init_db()

        job = Job(id="j-nourl", source="eures", title="Dev")
        storage.upsert_job(job)

        stored = storage.get_job_by_id("j-nourl")
        assert stored is not None
        assert stored.canonical_url is None
        assert stored.apply_url is None

    def test_normalize_job_preserves_url_through_storage(self, temp_db):
        """normalize_job() must carry 'url' through to canonical_url in the stored job."""
        from src.pipeline.normalize import normalize_job

        storage = Storage(temp_db)
        storage.init_db()

        job = normalize_job("arbeitnow", {
            "title": "Python Developer",
            "description": "Write Python",
            "url": "https://www.arbeitnow.com/jobs/python-dev-456",
        })
        storage.upsert_job(job)

        stored = storage.get_job_by_id(job.id)
        assert stored is not None
        assert stored.canonical_url == "https://www.arbeitnow.com/jobs/python-dev-456"

    def test_init_db_migrates_legacy_table_missing_canonical_url(self, temp_db):
        """init_db() adds canonical_url/apply_url to pre-existing tables that lack them."""
        import sqlite3
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        conn = sqlite3.connect(temp_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                description TEXT,
                location TEXT,
                countries TEXT NOT NULL DEFAULT '[]',
                remote INTEGER NOT NULL DEFAULT 0,
                employment_type TEXT,
                salary_min REAL, salary_max REAL,
                salary_currency TEXT NOT NULL DEFAULT 'EUR',
                published_at TEXT, updated_at TEXT,
                discovered_at TEXT NOT NULL,
                skills TEXT NOT NULL DEFAULT '[]',
                raw_payload TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT, last_seen_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                source_job_id TEXT, company TEXT,
                department TEXT, work_mode TEXT
            )
        """)
        conn.execute(
            "INSERT INTO jobs (id, source, discovered_at) VALUES (?, ?, ?)",
            ("j-old", "legacy", now),
        )
        conn.commit()
        conn.close()

        storage = Storage(temp_db)
        storage.init_db()

        new_job = Job(
            id="j-new-url",
            source="arbeitnow",
            title="New Job",
            canonical_url="https://example.com/job/1",
        )
        storage.upsert_job(new_job)
        stored = storage.get_job_by_id("j-new-url")
        assert stored is not None
        assert stored.canonical_url == "https://example.com/job/1"
        storage.close()
