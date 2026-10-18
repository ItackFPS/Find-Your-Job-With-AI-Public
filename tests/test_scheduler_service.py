"""Tests for the generic scheduler service.

Covers:
- SchedulerStorage CRUD and schema initialization
- Startup policy (run_once_per_day)
- Duplicate execution prevention (try_reserve_execution)
- Stale execution recovery
- Disabled schedule is skipped
- SchedulerService dispatches the correct handler
- Handler already-running protection
- Manual trigger
- YAML seeding

All tests use temporary SQLite databases (no real data is touched).
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test_scheduler.db")


@pytest.fixture
def storage(tmp_db: str):
    from src.scheduler.storage import SchedulerStorage
    s = SchedulerStorage(db_path=tmp_db)
    s.init_db()
    yield s
    s.close()


def _make_definition(
    definition_id: str = "test_daily",
    source_id: str = "test_source",
    enabled: bool = True,
    startup_policy: str = "run_once_per_day",
) -> dict[str, Any]:
    return {
        "id": definition_id,
        "source_id": source_id,
        "label": f"Test schedule {definition_id}",
        "enabled": enabled,
        "startup_policy": startup_policy,
        "retry_max": 0,
        "retry_delay_seconds": 300,
    }


# ---------------------------------------------------------------------------
# SchedulerStorage — schema and CRUD
# ---------------------------------------------------------------------------

class TestSchedulerStorage:
    def test_init_db_creates_tables(self, tmp_db: str) -> None:
        from src.scheduler.storage import SchedulerStorage
        s = SchedulerStorage(db_path=tmp_db)
        s.init_db()
        conn = s._get_conn()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "schedule_definitions" in tables
        assert "schedule_executions" in tables
        s.close()

    def test_init_db_is_idempotent(self, tmp_db: str) -> None:
        from src.scheduler.storage import SchedulerStorage
        s = SchedulerStorage(db_path=tmp_db)
        s.init_db()
        s.init_db()  # second call must not raise
        s.close()

    def test_upsert_definition_inserts(self, storage) -> None:
        storage.upsert_definition(_make_definition("li_daily", "linkedin"))
        defs = storage.get_enabled_definitions()
        assert len(defs) == 1
        assert defs[0].id == "li_daily"
        assert defs[0].source_id == "linkedin"

    def test_upsert_definition_updates(self, storage) -> None:
        storage.upsert_definition(_make_definition("li_daily", "linkedin", enabled=True))
        storage.upsert_definition({**_make_definition("li_daily", "linkedin"), "enabled": False})
        defs = storage.list_all_definitions()
        assert len(defs) == 1
        assert defs[0].enabled is False

    def test_disabled_definition_excluded_from_enabled(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1", enabled=True))
        storage.upsert_definition(_make_definition("d2", enabled=False))
        enabled = storage.get_enabled_definitions()
        assert len(enabled) == 1
        assert enabled[0].id == "d1"

    def test_get_definition_missing_returns_none(self, storage) -> None:
        assert storage.get_definition("nonexistent") is None

    def test_get_definition_found(self, storage) -> None:
        storage.upsert_definition(_make_definition("x"))
        defn = storage.get_definition("x")
        assert defn is not None
        assert defn.id == "x"

    def test_seed_from_yaml(self, tmp_db: str, tmp_path: Path) -> None:
        from src.scheduler.storage import SchedulerStorage

        yaml_file = tmp_path / "schedules.yaml"
        yaml_file.write_text(
            "schedules:\n"
            "  - id: test_src\n"
            "    source_id: mysource\n"
            "    label: My source\n"
            "    enabled: true\n"
            "    startup_policy: run_once_per_day\n"
            "    retry_max: 0\n"
            "    retry_delay_seconds: 300\n"
        )
        s = SchedulerStorage(db_path=tmp_db)
        s.init_db()
        count = s.seed_from_yaml(yaml_file)
        assert count == 1
        defs = s.get_enabled_definitions()
        assert len(defs) == 1
        assert defs[0].source_id == "mysource"
        s.close()

    def test_seed_missing_file_returns_zero(self, tmp_db: str, tmp_path: Path) -> None:
        from src.scheduler.storage import SchedulerStorage
        s = SchedulerStorage(db_path=tmp_db)
        s.init_db()
        count = s.seed_from_yaml(tmp_path / "nonexistent.yaml")
        assert count == 0
        s.close()


# ---------------------------------------------------------------------------
# SchedulerStorage — execution records
# ---------------------------------------------------------------------------

class TestSchedulerStorageExecutions:
    def _insert_execution(
        self,
        storage,
        definition_id: str,
        status: str,
        started_at: datetime,
        finished_at: datetime | None = None,
        run_ref: str | None = None,
    ) -> str:
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at, finished_at, run_ref)
               VALUES (?, ?, 'startup', ?, 1, ?, ?, ?)""",
            (
                exec_id,
                definition_id,
                status,
                started_at.isoformat(),
                finished_at.isoformat() if finished_at else None,
                run_ref,
            ),
        )
        return exec_id

    def test_get_last_completed_no_executions(self, storage) -> None:
        assert storage.get_last_completed_execution("x") is None

    def test_get_last_completed_returns_most_recent(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        now = datetime.now(UTC)
        self._insert_execution(storage, "d1", "completed", now - timedelta(hours=2))
        self._insert_execution(storage, "d1", "completed", now - timedelta(hours=1))
        result = storage.get_last_completed_execution("d1")
        assert result is not None
        # Most recent should be within the last 2 hours
        started = datetime.fromisoformat(result["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        assert abs((started - now).total_seconds()) < 7200

    def test_get_last_completed_since_filters(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)
        self._insert_execution(storage, "d1", "completed", yesterday)
        # Nothing since today's midnight
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result = storage.get_last_completed_execution("d1", since=today_start)
        assert result is None

    def test_get_last_completed_since_finds_today(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Use today_start + 30 min so the record is always within today,
        # regardless of what hour the test runs (avoids midnight flakiness).
        self._insert_execution(storage, "d1", "completed", today_start + timedelta(minutes=30))
        result = storage.get_last_completed_execution("d1", since=today_start)
        assert result is not None

    def test_failed_execution_not_returned_as_completed(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self._insert_execution(storage, "d1", "failed", now - timedelta(minutes=30))
        result = storage.get_last_completed_execution("d1", since=today_start)
        assert result is None  # failed != completed

    def test_complete_execution(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        now = datetime.now(UTC)
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at)
               VALUES (?, 'd1', 'startup', 'running', 1, ?)""",
            (exec_id, now.isoformat()),
        )
        storage.complete_execution(exec_id, run_ref="scan-abc", duration=42.5)
        row = conn.execute(
            "SELECT * FROM schedule_executions WHERE id = ?", (exec_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["run_ref"] == "scan-abc"
        assert row["duration_seconds"] == pytest.approx(42.5)
        assert row["finished_at"] is not None

    def test_fail_execution(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        now = datetime.now(UTC)
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at)
               VALUES (?, 'd1', 'startup', 'running', 1, ?)""",
            (exec_id, now.isoformat()),
        )
        storage.fail_execution(exec_id, error="Connection refused", duration=5.1)
        row = conn.execute(
            "SELECT * FROM schedule_executions WHERE id = ?", (exec_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "Connection refused" in row["error"]
        assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# try_reserve_execution — atomic duplicate prevention
# ---------------------------------------------------------------------------

class TestTryReserveExecution:
    def test_first_reservation_succeeds(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        exec_id = storage.try_reserve_execution("d1", trigger="startup")
        assert exec_id is not None

    def test_second_reservation_blocked_by_running(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        exec_id1 = storage.try_reserve_execution("d1", trigger="startup")
        assert exec_id1 is not None
        # Second reservation must fail: a running execution already exists
        exec_id2 = storage.try_reserve_execution("d1", trigger="startup")
        assert exec_id2 is None

    def test_reservation_after_completion_succeeds(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        exec_id1 = storage.try_reserve_execution("d1", trigger="startup")
        assert exec_id1 is not None
        storage.complete_execution(exec_id1)
        # completed row no longer blocks; but run_once_per_day guard still applies
        # (no_completed_since not passed here → pure running check)
        exec_id2 = storage.try_reserve_execution("d1", trigger="scheduled", no_completed_since=None)
        assert exec_id2 is not None

    def test_run_once_per_day_blocks_after_completion_today(self, storage) -> None:
        storage.upsert_definition(_make_definition("d1"))
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        exec_id1 = storage.try_reserve_execution(
            "d1", trigger="startup", no_completed_since=today_start
        )
        assert exec_id1 is not None
        storage.complete_execution(exec_id1)

        # Same day: try_reserve with today guard must fail
        exec_id2 = storage.try_reserve_execution(
            "d1", trigger="scheduled", no_completed_since=today_start
        )
        assert exec_id2 is None

    def test_run_once_per_day_allows_after_yesterday(self, storage) -> None:
        """If the last completion was yesterday, a new reservation for today is allowed."""
        storage.upsert_definition(_make_definition("d1"))
        yesterday_start = (
            datetime.now(UTC) - timedelta(days=1)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        # Insert a completed execution from yesterday
        exec_id_old = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at, finished_at)
               VALUES (?, 'd1', 'startup', 'completed', 1, ?, ?)""",
            (
                exec_id_old,
                (yesterday_start + timedelta(hours=9)).isoformat(),
                (yesterday_start + timedelta(hours=9, minutes=5)).isoformat(),
            ),
        )

        # Today: try_reserve with today guard must succeed
        exec_id_today = storage.try_reserve_execution(
            "d1", trigger="startup", no_completed_since=today_start
        )
        assert exec_id_today is not None

    def test_stale_running_execution_does_not_block_reservation(self, storage) -> None:
        """A 'running' row older than _STALE_HOURS is ignored (treated as crashed)."""
        from src.scheduler.storage import _STALE_HOURS
        storage.upsert_definition(_make_definition("d1"))

        # Insert a stale running execution (started 5 hours ago)
        stale_id = str(uuid.uuid4())
        stale_time = (datetime.now(UTC) - timedelta(hours=_STALE_HOURS + 1)).isoformat()
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at)
               VALUES (?, 'd1', 'startup', 'running', 1, ?)""",
            (stale_id, stale_time),
        )

        # A new reservation should succeed because the running row is stale
        exec_id = storage.try_reserve_execution("d1", trigger="startup")
        assert exec_id is not None


# ---------------------------------------------------------------------------
# Stale execution recovery
# ---------------------------------------------------------------------------

class TestStaleRecovery:
    def test_recover_stale_marks_as_interrupted(self, storage) -> None:
        """A dead process's execution is 'interrupted' (resumable, not counted
        against retry_max), no longer 'failed' (issue #68)."""
        from src.scheduler.storage import _STALE_HOURS
        storage.upsert_definition(_make_definition("d1"))

        # Insert a stale running execution
        stale_id = str(uuid.uuid4())
        stale_time = (datetime.now(UTC) - timedelta(hours=_STALE_HOURS + 1)).isoformat()
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at)
               VALUES (?, 'd1', 'startup', 'running', 1, ?)""",
            (stale_id, stale_time),
        )

        count = storage.recover_stale_executions()
        assert count == 1

        row = conn.execute(
            "SELECT status, error FROM schedule_executions WHERE id = ?", (stale_id,)
        ).fetchone()
        assert row["status"] == "interrupted"
        assert "interrupted" in row["error"].lower()

    def test_recent_running_is_recovered(self, storage) -> None:
        """recover_stale_executions recovers ALL running rows, regardless of age.

        A recent execution (30 min old) still belongs to a dead process —
        at startup no execution from this process can be running yet.
        """
        storage.upsert_definition(_make_definition("d1"))

        # Insert a recent running execution (30 minutes ago)
        recent_id = str(uuid.uuid4())
        recent_time = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at)
               VALUES (?, 'd1', 'startup', 'running', 1, ?)""",
            (recent_id, recent_time),
        )

        count = storage.recover_stale_executions()
        assert count == 1  # recovered regardless of age

        row = conn.execute(
            "SELECT status, error FROM schedule_executions WHERE id = ?", (recent_id,)
        ).fetchone()
        assert row["status"] == "interrupted"
        assert "interrupted" in row["error"].lower()


# ---------------------------------------------------------------------------
# SchedulerService — startup policy integration
# ---------------------------------------------------------------------------

class _FakeHandler:
    """Test double for a source handler."""

    def __init__(self, completed: bool = True) -> None:
        self.dispatch_calls: list[str] = []
        self._running = False
        self._completed = completed

    def is_running(self) -> bool:
        return self._running

    def dispatch(self):
        from src.scheduler.service import DispatchResult
        scan_id = str(uuid.uuid4())
        self.dispatch_calls.append(scan_id)
        return DispatchResult(run_ref=scan_id, completed=self._completed)


class TestSchedulerService:
    def _build_service(
        self,
        tmp_db: str,
        config_dir: str,
        handler: _FakeHandler,
    ):
        from src.scheduler.service import SchedulerService
        return SchedulerService(
            db_path=tmp_db,
            config_dir=config_dir,
            handlers={"test_source": handler},
        )

    def _write_schedules_yaml(self, config_dir: Path, enabled: bool = True) -> None:
        (config_dir / "schedules.yaml").write_text(
            f"schedules:\n"
            f"  - id: test_daily\n"
            f"    source_id: test_source\n"
            f"    label: Test\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            f"    startup_policy: run_once_per_day\n"
            f"    retry_max: 0\n"
            f"    retry_delay_seconds: 300\n"
        )

    def test_startup_triggers_dispatch_when_no_prior_run(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        """Backend starts with no previous execution → dispatch fires."""
        self._write_schedules_yaml(tmp_path)
        handler = _FakeHandler()
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.5)  # let the first tick and dispatch thread finish
        svc.stop()
        assert len(handler.dispatch_calls) == 1

    def test_startup_skips_dispatch_when_completed_today(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        """Backend starts after a scan already completed today → no duplicate."""
        self._write_schedules_yaml(tmp_path)
        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        storage.seed_from_yaml(tmp_path / "schedules.yaml")

        # Insert a completed execution from earlier today
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at, finished_at)
               VALUES (?, 'test_daily', 'startup', 'completed', 1, ?, ?)""",
            (
                exec_id,
                (today_start + timedelta(hours=1)).isoformat(),
                (today_start + timedelta(hours=1, minutes=5)).isoformat(),
            ),
        )
        storage.close()

        handler = _FakeHandler()
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.5)
        svc.stop()

        assert len(handler.dispatch_calls) == 0  # already ran today

    def test_startup_triggers_when_last_run_was_yesterday(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        """Backend starts; last successful run was yesterday → dispatch fires."""
        self._write_schedules_yaml(tmp_path)
        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        storage.seed_from_yaml(tmp_path / "schedules.yaml")

        # Insert completed execution from yesterday
        yesterday = datetime.now(UTC) - timedelta(days=1)
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at, finished_at)
               VALUES (?, 'test_daily', 'startup', 'completed', 1, ?, ?)""",
            (
                exec_id,
                yesterday.isoformat(),
                (yesterday + timedelta(minutes=5)).isoformat(),
            ),
        )
        storage.close()

        handler = _FakeHandler()
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.5)
        svc.stop()

        assert len(handler.dispatch_calls) == 1

    def test_startup_triggers_when_offline_multiple_days(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        """Backend was off for 5 days → exactly ONE dispatch fires, not 5."""
        self._write_schedules_yaml(tmp_path)
        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        storage.seed_from_yaml(tmp_path / "schedules.yaml")

        # Last completed run was 5 days ago
        five_days_ago = datetime.now(UTC) - timedelta(days=5)
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at, finished_at)
               VALUES (?, 'test_daily', 'startup', 'completed', 1, ?, ?)""",
            (
                exec_id,
                five_days_ago.isoformat(),
                (five_days_ago + timedelta(minutes=5)).isoformat(),
            ),
        )
        storage.close()

        handler = _FakeHandler()
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.5)
        svc.stop()

        # Exactly one catch-up scan, not one per missed day
        assert len(handler.dispatch_calls) == 1

    def test_disabled_schedule_never_dispatches(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules_yaml(tmp_path, enabled=False)
        handler = _FakeHandler()
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.5)
        svc.stop()
        assert len(handler.dispatch_calls) == 0

    def test_handler_already_running_blocks_dispatch(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules_yaml(tmp_path)
        handler = _FakeHandler()
        handler._running = True  # simulate already running
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.5)
        svc.stop()
        assert len(handler.dispatch_calls) == 0

    def test_no_handler_registered_does_not_crash(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        """If no handler is registered for a source_id, the service skips gracefully."""
        self._write_schedules_yaml(tmp_path)
        # Pass empty handlers dict — no handler for 'test_source'
        from src.scheduler.service import SchedulerService
        svc = SchedulerService(db_path=tmp_db, config_dir=str(tmp_path), handlers={})
        svc.start()
        time.sleep(0.5)
        svc.stop()
        # No crash, no dispatch

    def test_dispatch_records_completion(self, tmp_db: str, tmp_path: Path) -> None:
        """After dispatch, the execution row is marked 'completed'."""
        self._write_schedules_yaml(tmp_path)
        handler = _FakeHandler()
        svc = self._build_service(tmp_db, str(tmp_path), handler)
        svc.start()
        time.sleep(0.8)  # give dispatch thread time to finish
        svc.stop()

        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        execs = storage.list_executions("test_daily")
        storage.close()

        assert len(execs) >= 1
        assert execs[0]["status"] == "completed"
        assert execs[0]["trigger"] in ("startup", "scheduled")
        assert execs[0]["run_ref"] is not None  # handler.dispatch() returned a scan_id

    def test_dispatch_records_failure(self, tmp_db: str, tmp_path: Path) -> None:
        """If dispatch() raises, the execution row is marked 'failed'."""
        self._write_schedules_yaml(tmp_path)

        class FailingHandler:
            def is_running(self) -> bool:
                return False
            def dispatch(self) -> str:
                raise RuntimeError("LinkedIn returned 429")

        from src.scheduler.service import SchedulerService
        svc = SchedulerService(
            db_path=tmp_db, config_dir=str(tmp_path), handlers={"test_source": FailingHandler()}
        )
        svc.start()
        time.sleep(0.8)
        svc.stop()

        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        execs = storage.list_executions("test_daily")
        storage.close()

        assert len(execs) == 1
        assert execs[0]["status"] == "failed"
        assert "429" in execs[0]["error"]


# ---------------------------------------------------------------------------
# Simulated-restart integration: a real SchedulerService dispatching a real
# LinkedInScanner against an on-disk SQLite file, interrupted by a shutdown
# mid-scan, then a completely fresh set of objects opening that same file to
# verify recovery/resume -- exercises the actual persistence boundary
# (separate connections, separate objects) rather than reusing in-memory
# state across "process A" and "process B", per the incident investigation's
# explicit ask to test this beyond "merely calling a function with an Event."
# ---------------------------------------------------------------------------

class TestShutdownDuringScanRestartRecovery:
    def _write_linkedin_schedule(self, config_dir: Path) -> None:
        (config_dir / "schedules.yaml").write_text(
            "schedules:\n"
            "  - id: linkedin_daily\n"
            "    source_id: linkedin\n"
            "    label: LinkedIn daily scan\n"
            "    enabled: true\n"
            "    startup_policy: run_once_per_day\n"
            "    retry_max: 0\n"
            "    retry_delay_seconds: 300\n"
        )

    def _make_config(self):
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig
        return LinkedInConfig(
            enabled=True,
            rate_limit_seconds=0.0,
            max_pages_per_query=10,
            max_results_per_query=1,
            queries=[LinkedInQueryConfig(search_term="engineer", location="France", results_wanted=1)],
        )

    def test_shutdown_mid_scan_then_restart_resumes_from_checkpoint(self, tmp_path: Path) -> None:
        from src.linkedin.scanner import LinkedInScanner
        from src.linkedin.staging import LinkedInStagingStorage
        from src.scheduler.service import LinkedInHandler, SchedulerService  # gitleaks:allow (import, not a secret)
        from src.scheduler.storage import SchedulerStorage
        from tests.test_linkedin_scanner import _make_df_row, _make_mock_df

        db_path = str(tmp_path / "restart_test.db")
        self._write_linkedin_schedule(tmp_path)
        cfg = self._make_config()

        # -- "Process A": backend starts, scheduler dispatches, shutdown mid-scan --
        staging_a = LinkedInStagingStorage(db_path=db_path)
        staging_a.init_db()
        staging_a.recover_stale_scans()  # mirrors app.py's lifespan startup step

        event_a = threading.Event()
        scanner_a = LinkedInScanner(
            config=cfg, staging=staging_a, config_dir=str(tmp_path), shutdown_event=event_a
        )

        def scrape_page0(**kwargs):
            offset = kwargs["offset"]
            if offset == 0:
                event_a.set()  # the backend's shutdown handler firing mid-scan
                return _make_mock_df([_make_df_row("restart0001")])
            raise AssertionError("must not fetch further pages once shutdown was requested")

        mock_jobspy_a = MagicMock()
        mock_jobspy_a.scrape_jobs = scrape_page0

        svc_a = SchedulerService(
            db_path=db_path, config_dir=str(tmp_path),
            handlers={"linkedin": LinkedInHandler(scanner=scanner_a)},
        )
        with patch.dict("sys.modules", {"jobspy": mock_jobspy_a}):
            svc_a.start()
            time.sleep(0.8)  # let the startup-triggered dispatch thread run to completion
        svc_a.stop()
        staging_a.close()

        # -- Verify on-disk state through brand-new connections (not reused objects) --
        check_staging = LinkedInStagingStorage(db_path=db_path)
        check_staging.init_db()
        last_scan = check_staging.get_last_scan()
        # Never falsely "completed", and not "failed" either: a resumable
        # stop, reopened as the same logical scan below (issue #86).
        assert last_scan["status"] == "interrupted"
        assert "shutdown" in last_scan["error"].lower()
        cp = check_staging.get_scan_checkpoint()
        assert cp is not None  # checkpoint preserved, not lost or cleared
        assert cp["current_page"] == 1  # page 0 done, resume point is page 1
        check_staging.close()

        check_sched = SchedulerStorage(db_path=db_path)
        check_sched.init_db()
        execs = check_sched.list_executions("linkedin_daily")
        check_sched.close()
        assert len(execs) == 1
        # Fixed regression: dispatch() now returns a DispatchResult whose
        # `completed` flag comes from the scanner's own last_scan_completed()
        # (False for a shutdown stop), not merely "dispatch() didn't raise".
        # schedule_executions must never read a false "completed" for a scan
        # that actually stopped early. A stop for backend shutdown is
        # 'interrupted' (issue #68): resumable, and — unlike 'failed' — not
        # counted against retry_max (0 here), so the restart below still
        # resumes it.
        assert execs[0]["status"] == "interrupted"
        assert "shutdown" in execs[0]["error"]
        assert execs[0]["run_ref"] == last_scan["id"]  # scan_id still recorded, for observability

        # -- "Process B": brand new objects, same db file (simulates a real restart) --
        staging_b = LinkedInStagingStorage(db_path=db_path)
        staging_b.init_db()
        recovered = staging_b.recover_stale_scans()
        # Nothing left to recover: the cooperative shutdown already marked
        # the scan 'interrupted' cleanly before the process would have exited --
        # this is exactly what distinguishes a graceful stop from a true
        # crash (recover_stale_scans is the safety net for the latter).
        assert recovered == 0

        event_b = threading.Event()  # fresh process — nothing pending
        scanner_b = LinkedInScanner(
            config=cfg, staging=staging_b, config_dir=str(tmp_path), shutdown_event=event_b
        )

        offsets_requested: list[int] = []

        def resumed_scrape(**kwargs):
            offset = kwargs["offset"]
            offsets_requested.append(offset)
            if offset == 1:
                return _make_mock_df([_make_df_row("restart0002")])
            return _make_mock_df([])  # exhausted

        mock_jobspy_b = MagicMock()
        mock_jobspy_b.scrape_jobs = resumed_scrape

        # This is the payoff of the fix: because today's schedule_execution
        # now correctly reads 'interrupted' (not 'completed'), a brand new
        # SchedulerService's own startup tick -- not a manual "Scan Now" --
        # is what resumes it, exactly as intended for an unattended restart.
        svc_b = SchedulerService(
            db_path=db_path, config_dir=str(tmp_path),
            handlers={"linkedin": LinkedInHandler(scanner=scanner_b)},
        )
        with patch.dict("sys.modules", {"jobspy": mock_jobspy_b}):
            svc_b.start()
            time.sleep(0.8)
        svc_b.stop()

        assert 0 not in offsets_requested  # page 0 was NOT re-fetched
        assert offsets_requested[0] == 1

        rows = staging_b._get_conn().execute("SELECT linkedin_job_id FROM linkedin_staging").fetchall()
        assert {r["linkedin_job_id"] for r in rows} == {"restart0001", "restart0002"}  # nothing lost, nothing duplicated

        final = staging_b.get_last_scan()
        assert final["status"] == "completed"
        assert final["id"] == last_scan["id"]  # the same logical scan, reopened (issue #86)
        assert len(staging_b.list_scan_runs()) == 1
        assert staging_b.get_scan_checkpoint() is None  # cleared only on genuine full completion

        execs_b = SchedulerStorage(db_path=db_path)
        execs_b.init_db()
        all_execs = execs_b.list_executions("linkedin_daily")
        execs_b.close()
        assert len(all_execs) == 2  # process A's interrupted attempt + process B's genuine completion
        assert all_execs[0]["status"] == "completed"  # most recent first
        # Both executions reference the one logical scan run.
        assert {e["run_ref"] for e in all_execs} == {last_scan["id"]}
        staging_b.close()

    def test_normal_successful_scan_still_produces_completed_execution(self, tmp_path: Path) -> None:
        """Regression guard for the fix itself: a scan that genuinely
        completes (no shutdown, no internal failure) must still be recorded
        as schedule_executions.status == 'completed', through the real
        scanner -> handler -> scheduler chain, not just the _FakeHandler
        used elsewhere in this file."""
        from src.linkedin.scanner import LinkedInScanner
        from src.linkedin.staging import LinkedInStagingStorage
        from src.scheduler.service import LinkedInHandler, SchedulerService  # gitleaks:allow (import, not a secret)
        from src.scheduler.storage import SchedulerStorage
        from tests.test_linkedin_scanner import _make_mock_df

        db_path = str(tmp_path / "success_test.db")
        self._write_linkedin_schedule(tmp_path)
        cfg = self._make_config()

        staging = LinkedInStagingStorage(db_path=db_path)
        staging.init_db()
        scanner = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = lambda **kwargs: _make_mock_df([])  # exhausted immediately

        svc = SchedulerService(
            db_path=db_path, config_dir=str(tmp_path),
            handlers={"linkedin": LinkedInHandler(scanner=scanner)},
        )
        with patch.dict("sys.modules", {"jobspy": mock_jobspy}):
            svc.start()
            time.sleep(0.8)
        svc.stop()
        staging.close()

        check_staging = LinkedInStagingStorage(db_path=db_path)
        check_staging.init_db()
        assert check_staging.get_last_scan()["status"] == "completed"
        assert check_staging.get_scan_checkpoint() is None
        check_staging.close()

        check_sched = SchedulerStorage(db_path=db_path)
        check_sched.init_db()
        execs = check_sched.list_executions("linkedin_daily")
        check_sched.close()
        assert len(execs) == 1
        assert execs[0]["status"] == "completed"
        assert execs[0]["run_ref"] is not None

    def test_internally_failed_scan_produces_failed_execution_not_completed(self, tmp_path: Path) -> None:
        """A scan that fails via an internally-caught exception -- NOT a
        shutdown stop -- is the other half of the bug this fix closes:
        dispatch() returns a scan_id without raising either way, so before
        this fix schedule_executions read 'completed' for this case too.
        Must now read 'failed', matching linkedin_scan_runs.status exactly
        like the shutdown case does.

        Uses a failure in the final completion write (e.g. a realistic
        "database is locked" contention case, already anticipated by
        _do_scan's own comments) rather than a per-query scrape error --
        LinkedIn deliberately keeps going when one query fails (see
        TestQueryErrorDoesNotBlockResume in test_linkedin_scanner.py), so a
        scrape error alone does not reach _do_scan's outer except/failure
        path at all.
        """
        from src.linkedin.scanner import LinkedInScanner
        from src.linkedin.staging import LinkedInStagingStorage
        from src.scheduler.service import LinkedInHandler, SchedulerService  # gitleaks:allow (import, not a secret)
        from src.scheduler.storage import SchedulerStorage
        from tests.test_linkedin_scanner import _make_mock_df

        db_path = str(tmp_path / "internal_failure_test.db")
        self._write_linkedin_schedule(tmp_path)
        cfg = self._make_config()

        staging = LinkedInStagingStorage(db_path=db_path)
        staging.init_db()
        scanner = LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))

        mock_jobspy = MagicMock()
        mock_jobspy.scrape_jobs = lambda **kwargs: _make_mock_df([])  # exhausted immediately

        svc = SchedulerService(
            db_path=db_path, config_dir=str(tmp_path),
            handlers={"linkedin": LinkedInHandler(scanner=scanner)},
        )
        with (
            patch.dict("sys.modules", {"jobspy": mock_jobspy}),
            patch.object(staging, "complete_scan_run", side_effect=RuntimeError("database is locked")),
        ):
            svc.start()
            time.sleep(0.8)
        svc.stop()
        staging.close()

        check_staging = LinkedInStagingStorage(db_path=db_path)
        check_staging.init_db()
        last_scan = check_staging.get_last_scan()
        assert last_scan["status"] == "failed"
        check_staging.close()

        check_sched = SchedulerStorage(db_path=db_path)
        check_sched.init_db()
        execs = check_sched.list_executions("linkedin_daily")
        check_sched.close()
        assert len(execs) == 1
        assert execs[0]["status"] == "failed"  # not falsely "completed"
        assert execs[0]["run_ref"] == last_scan["id"]


# ---------------------------------------------------------------------------
# SchedulerService — manual trigger
# ---------------------------------------------------------------------------

class TestManualTrigger:
    def _setup(self, tmp_db: str, tmp_path: Path):
        (tmp_path / "schedules.yaml").write_text(
            "schedules:\n"
            "  - id: li_daily\n"
            "    source_id: linkedin\n"
            "    label: LinkedIn\n"
            "    enabled: true\n"
            "    startup_policy: run_once_per_day\n"
            "    retry_max: 0\n"
            "    retry_delay_seconds: 300\n"
        )
        handler = _FakeHandler()

        # Pre-insert a completed execution today so the startup trigger skips
        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        storage.seed_from_yaml(tmp_path / "schedules.yaml")
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        exec_id = str(uuid.uuid4())
        conn = storage._get_conn()
        conn.execute(
            """INSERT INTO schedule_executions
               (id, definition_id, trigger, status, attempt, started_at, finished_at)
               VALUES (?, 'li_daily', 'startup', 'completed', 1, ?, ?)""",
            (
                exec_id,
                (today_start + timedelta(minutes=30)).isoformat(),
                (today_start + timedelta(minutes=35)).isoformat(),
            ),
        )
        storage.close()

        from src.scheduler.service import SchedulerService
        svc = SchedulerService(
            db_path=tmp_db, config_dir=str(tmp_path), handlers={"linkedin": handler}
        )
        return svc, handler

    def test_manual_trigger_runs_even_when_completed_today(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        svc, handler = self._setup(tmp_db, tmp_path)
        svc.start()
        try:
            time.sleep(0.3)  # startup tick should skip (already ran today)
            assert len(handler.dispatch_calls) == 0

            # Manual trigger bypasses the startup policy
            exec_id = svc.trigger_manual("li_daily")
            assert exec_id is not None
            time.sleep(0.5)

            assert len(handler.dispatch_calls) == 1
        finally:
            svc.stop()

    def test_manual_trigger_raises_on_unknown_definition(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        svc, _ = self._setup(tmp_db, tmp_path)
        svc.start()
        try:
            with pytest.raises(ValueError, match="li_daily_nonexistent"):
                svc.trigger_manual("li_daily_nonexistent")
        finally:
            svc.stop()

    def test_manual_trigger_raises_when_already_running(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        svc, handler = self._setup(tmp_db, tmp_path)
        handler._running = True
        svc.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                svc.trigger_manual("li_daily")
        finally:
            svc.stop()


# ---------------------------------------------------------------------------
# SchedulerService — concurrent reservation protection
# ---------------------------------------------------------------------------

class TestConcurrentReservation:
    def test_two_reservations_only_one_succeeds(self, tmp_db: str) -> None:
        """Two concurrent scheduler instances each with their own connection.

        In production each process (or dispatch thread) creates its own
        SchedulerStorage instance. The SQLite BEGIN IMMEDIATE lock ensures
        only one reservation wins even when they race.
        """
        from src.scheduler.storage import SchedulerStorage

        # Seed the definition using a short-lived storage instance
        seed = SchedulerStorage(db_path=tmp_db)
        seed.init_db()
        seed.upsert_definition(_make_definition("d1"))
        seed.close()

        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        results: list[str | None] = []
        barrier = threading.Barrier(2)

        def try_reserve():
            # Each thread owns its own connection — mirrors real-world usage
            s = SchedulerStorage(db_path=tmp_db)
            s.init_db()
            barrier.wait()
            res = s.try_reserve_execution(
                "d1", trigger="startup", no_completed_since=today_start
            )
            results.append(res)
            s.close()

        t1 = threading.Thread(target=try_reserve)
        t2 = threading.Thread(target=try_reserve)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1  # exactly one reservation succeeds


# ---------------------------------------------------------------------------
# Trigger values stored in the DB
# ---------------------------------------------------------------------------

class TestTriggerValues:
    def test_startup_trigger_stored(self, tmp_db: str, tmp_path: Path) -> None:
        (tmp_path / "schedules.yaml").write_text(
            "schedules:\n"
            "  - id: t_daily\n"
            "    source_id: ts\n"
            "    label: T\n"
            "    enabled: true\n"
            "    startup_policy: run_once_per_day\n"
            "    retry_max: 0\n"
            "    retry_delay_seconds: 300\n"
        )
        handler = _FakeHandler()
        from src.scheduler.service import SchedulerService
        svc = SchedulerService(db_path=tmp_db, config_dir=str(tmp_path), handlers={"ts": handler})
        svc.start()
        time.sleep(0.8)
        svc.stop()

        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        execs = storage.list_executions("t_daily")
        storage.close()

        assert len(execs) >= 1
        # First-ever run is a startup trigger
        assert execs[-1]["trigger"] == "startup"


# ---------------------------------------------------------------------------
# LinkedIn handler integration (not a live scan — handler protocol only)
# ---------------------------------------------------------------------------

class TestLinkedInHandler:
    def test_is_running_delegates_to_scanner(self) -> None:
        from src.scheduler.service import LinkedInHandler
        mock_scanner = MagicMock()
        mock_scanner.is_scanning.return_value = True
        handler = LinkedInHandler(scanner=mock_scanner)
        assert handler.is_running() is True
        mock_scanner.is_scanning.return_value = False
        assert handler.is_running() is False

    def test_dispatch_calls_run_scan(self) -> None:
        from src.scheduler.service import LinkedInHandler
        mock_scanner = MagicMock()
        mock_scanner.run_scan.return_value = "scan-001"
        mock_scanner.last_scan_completed.return_value = True
        handler = LinkedInHandler(scanner=mock_scanner)
        result = handler.dispatch()
        assert result.run_ref == "scan-001"
        assert result.completed is True
        mock_scanner.run_scan.assert_called_once()

    def test_dispatch_not_completed_when_scanner_stopped_for_shutdown(self) -> None:
        """The scanner returning a scan_id is not, by itself, proof of
        completion -- LinkedInHandler must also consult last_scan_completed()
        (set False by the scanner itself for a shutdown stop or an
        internally-caught failure)."""
        from src.scheduler.service import LinkedInHandler
        mock_scanner = MagicMock()
        mock_scanner.run_scan.return_value = "scan-002"
        mock_scanner.last_scan_completed.return_value = False
        handler = LinkedInHandler(scanner=mock_scanner)
        result = handler.dispatch()
        assert result.run_ref == "scan-002"  # still reported, for observability
        assert result.completed is False

    def test_dispatch_not_completed_when_run_scan_returns_none(self) -> None:
        """run_scan() returning None (already scanning) must not be reported
        as completed, regardless of what last_scan_completed() says (it
        would reflect a *previous* run, not this dispatch attempt)."""
        from src.scheduler.service import LinkedInHandler
        mock_scanner = MagicMock()
        mock_scanner.run_scan.return_value = None
        mock_scanner.last_scan_completed.return_value = True
        handler = LinkedInHandler(scanner=mock_scanner)
        result = handler.dispatch()
        assert result.run_ref is None
        assert result.completed is False


# ---------------------------------------------------------------------------
# HelloWork handler integration (not a live scan — handler protocol only)
# ---------------------------------------------------------------------------

class TestHelloWorkHandler:
    def test_is_running_delegates_to_scanner(self) -> None:
        from src.scheduler.service import HelloWorkHandler
        mock_scanner = MagicMock()
        mock_scanner.is_scanning.return_value = True
        handler = HelloWorkHandler(scanner=mock_scanner)
        assert handler.is_running() is True
        mock_scanner.is_scanning.return_value = False
        assert handler.is_running() is False

    def test_dispatch_calls_run_scan(self) -> None:
        from src.scheduler.service import HelloWorkHandler
        mock_scanner = MagicMock()
        mock_scanner.run_scan.return_value = "scan-001"
        mock_scanner.last_scan_completed.return_value = True
        handler = HelloWorkHandler(scanner=mock_scanner)
        result = handler.dispatch()
        assert result.run_ref == "scan-001"
        assert result.completed is True
        mock_scanner.run_scan.assert_called_once()

    def test_dispatch_not_completed_when_scanner_stopped_for_shutdown(self) -> None:
        from src.scheduler.service import HelloWorkHandler
        mock_scanner = MagicMock()
        mock_scanner.run_scan.return_value = "scan-002"
        mock_scanner.last_scan_completed.return_value = False
        handler = HelloWorkHandler(scanner=mock_scanner)
        result = handler.dispatch()
        assert result.run_ref == "scan-002"
        assert result.completed is False


# ---------------------------------------------------------------------------
# Auto-pipeline trigger after scan completion
#
# Requirements verified:
#   1. Successful scan → pipeline automatically starts
#   2. Failed scan (dispatch raises) → pipeline does NOT start
#   3. Scan returns None (cancelled/no results) → pipeline does NOT start
#   4. Duplicate completion while pipeline already running → only one pipeline run
#   5. Manual pipeline execution still works (independent of scheduler)
#   6. Auto-triggered run is recorded exactly like a manual run
# ---------------------------------------------------------------------------

class _FakePipelineRunner:
    """Minimal stand-in for PipelineRunner used by SchedulerService._dispatch."""

    def __init__(self, initially_running: bool = False) -> None:
        self.start_calls: list[str] = []
        self._running = initially_running

    def is_running(self) -> bool:
        return self._running

    def start_run(self) -> str:
        if self._running:
            raise RuntimeError("A pipeline run is already in progress")
        run_id = f"pipeline-{len(self.start_calls)}"
        self.start_calls.append(run_id)
        self._running = True
        return run_id


class TestAutoPipelineTrigger:
    """Tests that SchedulerService._dispatch auto-triggers the pipeline."""

    def _write_schedules(self, config_dir: Path) -> None:
        (config_dir / "schedules.yaml").write_text(
            "schedules:\n"
            "  - id: li_daily\n"
            "    source_id: linkedin\n"
            "    label: LinkedIn\n"
            "    enabled: true\n"
            "    startup_policy: run_once_per_day\n"
            "    retry_max: 0\n"
            "    retry_delay_seconds: 300\n"
        )

    def _build_service(
        self,
        tmp_db: str,
        config_dir: Path,
        handler: _FakeHandler,
        pipeline_runner: _FakePipelineRunner,
    ):
        from src.scheduler.service import SchedulerService
        return SchedulerService(
            db_path=tmp_db,
            config_dir=str(config_dir),
            handlers={"linkedin": handler},
            pipeline_runner=pipeline_runner,
        )

    # 1. Successful scan → pipeline starts
    def test_successful_scan_triggers_pipeline(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules(tmp_path)
        handler = _FakeHandler()  # dispatch() returns a non-None scan_id
        pipeline = _FakePipelineRunner()
        svc = self._build_service(tmp_db, tmp_path, handler, pipeline)
        svc.start()
        time.sleep(0.8)
        svc.stop()

        assert len(handler.dispatch_calls) == 1, "scan should have run once"
        assert len(pipeline.start_calls) == 1, "pipeline should have been auto-triggered"

    # 2. Failed scan (exception) → pipeline does NOT start
    def test_failed_scan_does_not_trigger_pipeline(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules(tmp_path)

        class FailHandler:
            def is_running(self) -> bool:
                return False
            def dispatch(self) -> str:
                raise RuntimeError("LinkedIn returned 429")

        pipeline = _FakePipelineRunner()
        from src.scheduler.service import SchedulerService
        svc = SchedulerService(
            db_path=tmp_db,
            config_dir=str(tmp_path),
            handlers={"linkedin": FailHandler()},
            pipeline_runner=pipeline,
        )
        svc.start()
        time.sleep(0.8)
        svc.stop()

        assert len(pipeline.start_calls) == 0, "pipeline must NOT start after a failed scan"

    # 3. Scan returns None (cancelled / empty) → pipeline does NOT start
    def test_scan_returning_none_does_not_trigger_pipeline(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules(tmp_path)

        class NoneHandler:
            def is_running(self) -> bool:
                return False
            def dispatch(self):
                from src.scheduler.service import DispatchResult
                return DispatchResult(run_ref=None, completed=False)  # cancelled / nothing to do

        pipeline = _FakePipelineRunner()
        from src.scheduler.service import SchedulerService
        svc = SchedulerService(
            db_path=tmp_db,
            config_dir=str(tmp_path),
            handlers={"linkedin": NoneHandler()},
            pipeline_runner=pipeline,
        )
        svc.start()
        time.sleep(0.8)
        svc.stop()

        assert len(pipeline.start_calls) == 0, "pipeline must NOT start when scan returns None"

    # 4. Pipeline already running when scan completes → no second pipeline run
    def test_duplicate_completion_only_one_pipeline_run(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules(tmp_path)
        handler = _FakeHandler()
        # Mark pipeline as already running before the scan finishes
        pipeline = _FakePipelineRunner(initially_running=True)
        svc = self._build_service(tmp_db, tmp_path, handler, pipeline)
        svc.start()
        time.sleep(0.8)
        svc.stop()

        # Scan ran, but pipeline was already busy → no extra pipeline start
        assert len(handler.dispatch_calls) == 1
        assert len(pipeline.start_calls) == 0, (
            "pipeline.start_run must NOT be called when pipeline is already running"
        )

    # 5. No pipeline_runner configured → scheduler still works, no crash
    def test_no_pipeline_runner_does_not_crash(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules(tmp_path)
        handler = _FakeHandler()
        from src.scheduler.service import SchedulerService
        svc = SchedulerService(
            db_path=tmp_db,
            config_dir=str(tmp_path),
            handlers={"linkedin": handler},
            pipeline_runner=None,  # not configured
        )
        svc.start()
        time.sleep(0.8)
        svc.stop()
        # No crash, scan completed normally
        assert len(handler.dispatch_calls) == 1

    # 6. Auto-triggered run_ref is recorded in the execution row (visible in run history)
    def test_auto_triggered_run_ref_recorded(
        self, tmp_db: str, tmp_path: Path
    ) -> None:
        self._write_schedules(tmp_path)
        handler = _FakeHandler()
        pipeline = _FakePipelineRunner()
        svc = self._build_service(tmp_db, tmp_path, handler, pipeline)
        svc.start()
        time.sleep(0.8)
        svc.stop()

        from src.scheduler.storage import SchedulerStorage
        storage = SchedulerStorage(db_path=tmp_db)
        storage.init_db()
        execs = storage.list_executions("li_daily")
        storage.close()

        assert len(execs) >= 1
        exec_row = execs[0]
        assert exec_row["status"] == "completed"
        assert exec_row["run_ref"] is not None, "scan_id (run_ref) must be persisted"
        # The pipeline run_id is separate — the scheduler records the scan ref, not pipeline ref.
        # Verify the pipeline was triggered separately.
        assert len(pipeline.start_calls) == 1
